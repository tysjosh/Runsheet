/**
 * Active route — the compartment manifest, the stop sequence, and the two
 * operational submissions that belong to a run.
 *
 * What this screen owes the driver:
 *
 *  - Per compartment: the identifier, the loaded grade, the loaded gallons, and
 *    the gallons remaining after each completed stop (R6.10). Every volume is
 *    labelled `gal` by `lib/units.ts` and converted nowhere (R16.18, R16.19).
 *  - A cross-contamination warning naming the compartment, the prior grade, and
 *    the current grade (R6.11).
 *  - A hard block on a check-in that draws from a compartment whose warning is
 *    unacknowledged and which records no cleaning event, showing the
 *    acknowledgement prompt instead (R6.12).
 *  - A terminal wait report carrying the driver-observed times, with no `source`
 *    field so the server default `driver_report` applies (R8.1).
 *  - A compartment cleaning event carrying the session `driver_id`, a method from
 *    `{flush, purge, sanitize}`, and evidence `file_ref` values (R8.2).
 *
 *  - The way in to the vehicle inspection form, pre-trip and post-trip
 *    (R8.3, R8.8). The form itself lives at `app/inspection/new.tsx`; this is
 *    where a driver working a run looks for it.
 *
 * Requirements: 6.10, 6.11, 6.12, 8.1, 8.2, 8.3, 8.8, 10.15, 16.13, 16.18, 16.19
 */

import { useNetInfo } from '@react-native-community/netinfo';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useRouter } from 'expo-router';
import { useEffect, useMemo, useState } from 'react';
import { Pressable, RefreshControl, ScrollView, View } from 'react-native';

import { PendingQueueChip } from '@/components/PendingQueueChip';
import { PermissionBanner } from '@/components/PermissionBanner';
import { PromptDialog } from '@/components/PromptDialog';
import { Button } from '@/components/ui/button';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Text } from '@/components/ui/text';
import {
  locationPermissionDecision,
  requestGeotag,
  type GeoFix,
} from '@/lib/geotag';
import {
  queueDepth,
  subscribeToQueue,
  type QueueDepth,
} from '@/lib/offline-queue';
import {
  CLEANING_METHOD_OPTIONS,
  queueCompartmentCleaning,
  queueTerminalWaitReport,
  waitMinutesBetween,
  type CleaningMethod,
} from '@/lib/ops-api';
import { queryKeys, WORK_SCOPE } from '@/lib/query-keys';
import {
  acknowledgeCompartment,
  acknowledgedCompartments,
  buildCompartmentLedger,
  crossContaminationMessage,
  evaluateCheckinGate,
  queueStopCheckin,
  type CompartmentLedgerRow,
} from '@/lib/route-api';
import { currentSessionIdentity } from '@/lib/session';
import { formatGallons, VOLUME_UNIT_LABEL } from '@/lib/units';
import { loadAssignedWork, loadWorkDetail } from '@/lib/work-api';
import type { RouteStop } from '@/types/order';

function stopLabel(stop: RouteStop): string {
  return `${stop.sequence + 1}. ${stop.station_id}`;
}

function localTime(value: string | null | undefined): string {
  if (!value) {
    return 'Not available';
  }
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? new Date(parsed).toLocaleString() : value;
}

export default function RouteScreen() {
  const queryClient = useQueryClient();
  const router = useRouter();
  const driverId = currentSessionIdentity()?.driverId ?? '';

  const [selectedOrderId, setSelectedOrderId] = useState<string | null>(null);
  const [acknowledged, setAcknowledged] = useState<Set<string>>(new Set());
  const [notice, setNotice] = useState<string | null>(null);
  const [blockedBy, setBlockedBy] = useState<CompartmentLedgerRow[]>([]);
  const [locationDenied, setLocationDenied] = useState(false);
  const [depth, setDepth] = useState<QueueDepth>({
    pending: 0,
    inFlight: 0,
    failed: 0,
    conflict: 0,
    outstanding: 0,
  });
  const network = useNetInfo();

  // Check-in state: the gallons the driver reports per grade for one stop.
  const [checkinStop, setCheckinStop] = useState<RouteStop | null>(null);
  const [checkinGallons, setCheckinGallons] = useState<Record<string, string>>({});
  const [checkinBusy, setCheckinBusy] = useState(false);

  // Cleaning-event state.
  const [cleaningFor, setCleaningFor] = useState<CompartmentLedgerRow | null>(
    null,
  );
  const [cleaningMethod, setCleaningMethod] = useState<CleaningMethod>('flush');
  const [cleaningNotes, setCleaningNotes] = useState('');

  // Terminal wait report state.
  const [terminalId, setTerminalId] = useState('');
  const [waitStart, setWaitStart] = useState<string | null>(null);
  const [waitEnd, setWaitEnd] = useState<string | null>(null);
  const [waitNotes, setWaitNotes] = useState('');
  const [waitPromptOpen, setWaitPromptOpen] = useState(false);

  useEffect(() => {
    setAcknowledged(acknowledgedCompartments());
    // R10.15 — the indicator is persistent, so the decision is read on mount
    // rather than only after a check-in has asked for a fix.
    void locationPermissionDecision().then((decision) => {
      setLocationDenied(decision === 'denied');
    });
    void queueDepth().then(setDepth).catch(() => undefined);
    return subscribeToQueue(setDepth);
  }, []);

  const work = useQuery({
    queryKey: queryKeys.work({
      statuses: ['dispatched', 'in_transit'],
      size: 50,
    }),
    queryFn: loadAssignedWork,
  });

  const orders = useMemo(() => {
    const list = [...(work.data?.data ?? [])];
    // The delivery being run comes first, then the rest in window order.
    return list.sort((left, right) => {
      if (left.status !== right.status) {
        return left.status === 'in_transit' ? -1 : 1;
      }
      return (
        new Date(left.delivery_window_start).getTime() -
        new Date(right.delivery_window_start).getTime()
      );
    });
  }, [work.data]);

  useEffect(() => {
    if (orders.length === 0) {
      return;
    }
    if (!orders.some((order) => order.order_id === selectedOrderId)) {
      setSelectedOrderId(orders[0].order_id);
    }
  }, [orders, selectedOrderId]);

  const detail = useQuery({
    queryKey: queryKeys.order(selectedOrderId ?? ''),
    queryFn: () => loadWorkDetail(selectedOrderId ?? ''),
    enabled: Boolean(selectedOrderId),
  });

  const order = detail.data;
  const ledger = useMemo(() => buildCompartmentLedger(order), [order]);
  const stops = useMemo(
    () => [...(order?.stops ?? [])].sort((a, b) => a.sequence - b.sequence),
    [order],
  );

  const acknowledge = (row: CompartmentLedgerRow) => {
    acknowledgeCompartment(row.compartmentId);
    setAcknowledged(acknowledgedCompartments());
    setBlockedBy((current) =>
      current.filter((entry) => entry.compartmentId !== row.compartmentId),
    );
    setNotice(
      `Compartment ${row.compartmentId} acknowledged. Record the cleaning event once it is done.`,
    );
  };

  const submitCleaning = async () => {
    if (!cleaningFor) {
      return;
    }
    if (!driverId) {
      setNotice('The driver session is unavailable, so nothing was recorded.');
      return;
    }
    try {
      await queueCompartmentCleaning({
        cleaning: {
          compartmentId: cleaningFor.compartmentId,
          method: cleaningMethod,
          driverId,
          notes: cleaningNotes,
        },
      });
      setNotice(
        `Cleaning event queued for compartment ${cleaningFor.compartmentId}.`,
      );
      setCleaningFor(null);
      setCleaningNotes('');
      await queryClient.invalidateQueries({ queryKey: [WORK_SCOPE] });
    } catch (error) {
      setNotice(
        error instanceof Error
          ? error.message
          : 'The cleaning event could not be recorded.',
      );
    }
  };

  const openCheckin = (stop: RouteStop) => {
    setNotice(null);
    setBlockedBy([]);
    setCheckinStop(stop);
    const seeded: Record<string, string> = {};
    Object.entries(stop.planned_gallons_by_grade ?? {}).forEach(
      ([grade, gallons]) => {
        seeded[grade] = String(gallons ?? '');
      },
    );
    setCheckinGallons(seeded);
  };

  const submitCheckin = async () => {
    if (!checkinStop || !order) {
      return;
    }
    const gallonsByGrade: Record<string, number> = {};
    for (const [grade, raw] of Object.entries(checkinGallons)) {
      const parsed = Number(raw);
      if (!Number.isFinite(parsed) || parsed < 0) {
        setNotice(`Enter the gallons dropped for ${grade}.`);
        return;
      }
      gallonsByGrade[grade] = parsed;
    }
    if (Object.keys(gallonsByGrade).length === 0) {
      setNotice('Enter the gallons dropped before checking in.');
      return;
    }

    // R6.12 — the gate runs before anything is queued.
    const gate = evaluateCheckinGate({
      rows: ledger,
      grades: Object.keys(gallonsByGrade),
      acknowledged,
    });
    if (gate.blocked) {
      setBlockedBy(gate.blockedBy);
      setNotice(
        'This check-in draws from a compartment with an unacknowledged ' +
          'cross-contamination warning. Acknowledge it, or record the cleaning event, first.',
      );
      return;
    }

    const planId = order.plan_id ?? '';
    const routeId = order.route_id ?? '';
    if (!planId || !routeId) {
      setNotice(
        'This assignment carries no plan reference, so the stop check-in is ' +
          'unavailable. Report the drop to dispatch on the order thread.',
      );
      return;
    }

    setCheckinBusy(true);
    try {
      const { fix, permission } = await requestGeotag();
      setLocationDenied(permission === 'denied');
      if (!fix) {
        setNotice(
          'A stop check-in has to carry a location, and none is available. ' +
            'Turn location on for this app, or report the drop to dispatch.',
        );
        return;
      }
      const geotag: GeoFix = { lat: fix.lat, lng: fix.lng };
      await queueStopCheckin({
        checkin: {
          planId,
          routeId,
          stationId: checkinStop.station_id,
          sequence: checkinStop.sequence,
          orderId: order.order_id,
          gallonsByGrade,
          geotag,
        },
      });
      setNotice(
        `Check-in queued for ${stopLabel(checkinStop)}. It sends as soon as there is service.`,
      );
      setCheckinStop(null);
      setCheckinGallons({});
      await queryClient.invalidateQueries({ queryKey: [WORK_SCOPE] });
    } catch (error) {
      setNotice(
        error instanceof Error
          ? error.message
          : 'The check-in could not be recorded.',
      );
    } finally {
      setCheckinBusy(false);
    }
  };

  const submitWaitReport = async () => {
    if (!terminalId.trim()) {
      setNotice('Enter the terminal you waited at.');
      return;
    }
    if (!waitStart || !waitEnd) {
      setNotice('Mark both the start and the end of the wait.');
      return;
    }
    if (!driverId) {
      setNotice('The driver session is unavailable, so nothing was recorded.');
      return;
    }
    try {
      await queueTerminalWaitReport({
        observation: {
          terminalId: terminalId.trim(),
          waitStart,
          waitEnd,
          driverId,
          notes: waitNotes,
        },
      });
      setNotice(
        `Wait report queued: ${waitMinutesBetween(waitStart, waitEnd)} minutes at ${terminalId.trim()}.`,
      );
      setWaitStart(null);
      setWaitEnd(null);
      setWaitNotes('');
    } catch (error) {
      setNotice(
        error instanceof Error
          ? error.message
          : 'The wait report could not be recorded.',
      );
    }
  };

  return (
    <ScrollView
      className="flex-1 bg-background"
      contentContainerClassName="gap-5 p-5 pb-28"
      keyboardShouldPersistTaps="handled"
      refreshControl={
        <RefreshControl
          refreshing={detail.isRefetching || work.isRefetching}
          onRefresh={() => {
            void work.refetch();
            void detail.refetch();
          }}
        />
      }
    >
      <View className="flex-row items-start justify-between gap-3">
        <View className="flex-1 gap-1">
          <Text className="text-3xl font-bold">Active route</Text>
          <Text className="text-muted-foreground">
            Compartment manifest and stop sequence, in {VOLUME_UNIT_LABEL}
          </Text>
        </View>
        <PendingQueueChip
          counts={depth}
          isOnline={network.isConnected !== false}
        />
      </View>

      {locationDenied && <PermissionBanner permissions={{ location: 'denied' }} />}

      {orders.length > 1 && (
        <View className="flex-row flex-wrap gap-2">
          {orders.map((entry) => {
            const active = entry.order_id === selectedOrderId;
            return (
              <Pressable
                key={entry.order_id}
                accessibilityRole="button"
                accessibilityState={{ selected: active }}
                onPress={() => setSelectedOrderId(entry.order_id)}
                className={
                  active
                    ? 'rounded-full bg-primary px-4 py-2'
                    : 'rounded-full border border-input px-4 py-2'
                }
              >
                <Text
                  className={
                    active
                      ? 'text-sm font-semibold text-primary-foreground'
                      : 'text-sm'
                  }
                >
                  {entry.customer_name}
                </Text>
              </Pressable>
            );
          })}
        </View>
      )}

      {notice && (
        <View className="rounded-xl bg-muted p-4">
          <Text>{notice}</Text>
        </View>
      )}

      {detail.isLoading && <Text>Loading the route…</Text>}

      {!detail.isLoading && orders.length === 0 && (
        <Card>
          <CardHeader>
            <CardTitle>No active route</CardTitle>
            <CardDescription>
              New work appears here as soon as dispatch approves it.
            </CardDescription>
          </CardHeader>
        </Card>
      )}

      {/* ---- compartment manifest (R6.10, R6.11) -------------------------- */}
      {order && (
        <View className="gap-3">
          <Text className="text-xl font-bold">Compartments</Text>
          {order.manifest_available === false || ledger.length === 0 ? (
            <Text className="text-muted-foreground">
              No compartment manifest is available for this assignment.
            </Text>
          ) : (
            ledger.map((row) => {
              const warning = crossContaminationMessage(row);
              const outstanding =
                row.crossContaminationWarning &&
                !row.cleaningRecorded &&
                !acknowledged.has(row.compartmentId);
              return (
                <Card
                  key={row.compartmentId}
                  className={outstanding ? 'border-red-300' : undefined}
                >
                  <CardHeader>
                    <CardTitle>Compartment {row.compartmentId}</CardTitle>
                    <CardDescription>
                      Loaded {row.loadedGrade} ·{' '}
                      {formatGallons(row.loadedGallons)}
                    </CardDescription>
                  </CardHeader>
                  <CardContent className="gap-2">
                    <Text className="font-semibold">
                      Remaining: {formatGallons(row.remainingGallons)}
                    </Text>
                    {row.draws.length > 0 && (
                      <View className="gap-1">
                        {row.draws.map((draw) => (
                          <Text
                            key={`${row.compartmentId}-${draw.sequence}`}
                            className="text-sm text-muted-foreground"
                          >
                            After stop {draw.sequence + 1} ({draw.stationId}):{' '}
                            {formatGallons(draw.remainingAfter)} left · drew{' '}
                            {formatGallons(draw.gallons)}
                          </Text>
                        ))}
                      </View>
                    )}
                    {warning && (
                      <Text className="font-semibold text-destructive">
                        {warning}
                      </Text>
                    )}
                    {row.cleaningRecorded && (
                      <Text className="text-sm text-muted-foreground">
                        Last cleaned {localTime(row.lastCleanedAt)}
                      </Text>
                    )}
                    {outstanding && (
                      <View className="gap-2">
                        <Button size="sm" onPress={() => acknowledge(row)}>
                          <Text>Acknowledge warning</Text>
                        </Button>
                        <Button
                          size="sm"
                          variant="outline"
                          onPress={() => {
                            setCleaningFor(row);
                            setCleaningMethod('flush');
                          }}
                        >
                          <Text>Record cleaning event</Text>
                        </Button>
                      </View>
                    )}
                    {!outstanding && row.crossContaminationWarning && (
                      <Text className="text-sm text-muted-foreground">
                        Warning acknowledged on this device.
                      </Text>
                    )}
                  </CardContent>
                </Card>
              );
            })
          )}
        </View>
      )}

      {/* ---- the cleaning-event form (R8.2) ------------------------------- */}
      {cleaningFor && (
        <Card>
          <CardHeader>
            <CardTitle>
              Cleaning event · compartment {cleaningFor.compartmentId}
            </CardTitle>
            <CardDescription>
              Recorded against your driver id from this session.
            </CardDescription>
          </CardHeader>
          <CardContent className="gap-3">
            <View className="gap-2">
              {CLEANING_METHOD_OPTIONS.map((option) => {
                const active = option.value === cleaningMethod;
                return (
                  <Pressable
                    key={option.value}
                    accessibilityRole="radio"
                    accessibilityState={{ selected: active }}
                    onPress={() => setCleaningMethod(option.value)}
                    className={
                      active
                        ? 'rounded-xl border-2 border-primary p-3'
                        : 'rounded-xl border border-input p-3'
                    }
                  >
                    <Text className="font-semibold">{option.label}</Text>
                    <Text className="text-sm text-muted-foreground">
                      {option.description}
                    </Text>
                  </Pressable>
                );
              })}
            </View>
            <Input
              value={cleaningNotes}
              onChangeText={setCleaningNotes}
              placeholder="Notes (optional)"
              multiline
            />
            <Button onPress={() => void submitCleaning()}>
              <Text>Queue cleaning event</Text>
            </Button>
            <Button variant="outline" onPress={() => setCleaningFor(null)}>
              <Text>Cancel</Text>
            </Button>
          </CardContent>
        </Card>
      )}

      {/* ---- stop sequence and check-in (R6.10, R6.12) -------------------- */}
      {order && (
        <View className="gap-3">
          <Text className="text-xl font-bold">Stops</Text>
          {order.route_available === false || stops.length === 0 ? (
            <Text className="text-muted-foreground">
              No route sequence is available for this assignment.
            </Text>
          ) : (
            stops.map((stop) => (
              <Card key={`${stop.sequence}-${stop.station_id}`}>
                <CardHeader>
                  <CardTitle>{stopLabel(stop)}</CardTitle>
                  <CardDescription>
                    {stop.status === 'completed'
                      ? 'Completed'
                      : `ETA ${localTime(stop.planned_arrival)}`}
                  </CardDescription>
                </CardHeader>
                <CardContent className="gap-2">
                  {Object.entries(stop.planned_gallons_by_grade ?? {}).map(
                    ([grade, gallons]) => (
                      <Text key={grade}>
                        {grade}: {formatGallons(gallons)} planned
                      </Text>
                    ),
                  )}
                  {stop.status !== 'completed' && (
                    <Button
                      variant="outline"
                      size="sm"
                      onPress={() => openCheckin(stop)}
                    >
                      <Text>Check in at this stop</Text>
                    </Button>
                  )}
                </CardContent>
              </Card>
            ))
          )}
        </View>
      )}

      {checkinStop && (
        <Card>
          <CardHeader>
            <CardTitle>Check in · {stopLabel(checkinStop)}</CardTitle>
            <CardDescription>
              Gallons that actually went into the tank. Nothing is converted.
            </CardDescription>
          </CardHeader>
          <CardContent className="gap-3">
            {Object.keys(checkinGallons).length === 0 && (
              <Text className="text-muted-foreground">
                This stop carries no planned grades, so there is nothing to report.
              </Text>
            )}
            {Object.keys(checkinGallons).map((grade) => (
              <View key={grade} className="gap-2">
                <Text className="font-medium">
                  {grade} ({VOLUME_UNIT_LABEL})
                </Text>
                <Input
                  value={checkinGallons[grade]}
                  onChangeText={(next) =>
                    setCheckinGallons((current) => ({
                      ...current,
                      [grade]: next,
                    }))
                  }
                  keyboardType="decimal-pad"
                  placeholder="0.0"
                  editable={!checkinBusy}
                />
              </View>
            ))}
            {blockedBy.length > 0 && (
              <View className="gap-2 rounded-xl border border-red-300 p-3">
                {blockedBy.map((row) => (
                  <View key={row.compartmentId} className="gap-2">
                    <Text className="font-semibold text-destructive">
                      {crossContaminationMessage(row)}
                    </Text>
                    <Button size="sm" onPress={() => acknowledge(row)}>
                      <Text>
                        Acknowledge compartment {row.compartmentId}
                      </Text>
                    </Button>
                  </View>
                ))}
              </View>
            )}
            <Button
              disabled={checkinBusy}
              onPress={() => void submitCheckin()}
            >
              <Text>{checkinBusy ? 'Recording…' : 'Submit check-in'}</Text>
            </Button>
            <Button
              variant="outline"
              disabled={checkinBusy}
              onPress={() => {
                setCheckinStop(null);
                setBlockedBy([]);
              }}
            >
              <Text>Cancel</Text>
            </Button>
          </CardContent>
        </Card>
      )}

      {/* ---- vehicle inspection (R8.3, R8.8) ----------------------------- */}
      <Card>
        <CardHeader>
          <CardTitle>Vehicle inspection</CardTitle>
          <CardDescription>
            Pre-trip and post-trip walk-arounds, with defects and photos.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Button
            variant="outline"
            onPress={() => router.push('/inspection/new')}
          >
            <Text>Start an inspection</Text>
          </Button>
        </CardContent>
      </Card>

      {/* ---- terminal wait report (R8.1) --------------------------------- */}
      <Card>
        <CardHeader>
          <CardTitle>Terminal wait</CardTitle>
          <CardDescription>
            Your observed times are what gets reported.
          </CardDescription>
        </CardHeader>
        <CardContent className="gap-3">
          <Input
            value={terminalId}
            onChangeText={setTerminalId}
            placeholder="Terminal id"
            autoCapitalize="none"
          />
          <View className="gap-2">
            <Text className="text-sm text-muted-foreground">
              Wait started: {waitStart ? localTime(waitStart) : 'not marked'}
            </Text>
            <View className="flex-row gap-2">
              <Button
                size="sm"
                variant="outline"
                className="flex-1"
                onPress={() => setWaitStart(new Date().toISOString())}
              >
                <Text>Mark start now</Text>
              </Button>
              <Button
                size="sm"
                variant="outline"
                className="flex-1"
                onPress={() => setWaitPromptOpen(true)}
              >
                <Text>Started N min ago</Text>
              </Button>
            </View>
          </View>
          <View className="gap-2">
            <Text className="text-sm text-muted-foreground">
              Wait ended: {waitEnd ? localTime(waitEnd) : 'not marked'}
            </Text>
            <Button
              size="sm"
              variant="outline"
              onPress={() => setWaitEnd(new Date().toISOString())}
            >
              <Text>Mark end now</Text>
            </Button>
          </View>
          <Input
            value={waitNotes}
            onChangeText={setWaitNotes}
            placeholder="Notes (optional)"
            multiline
          />
          <Button onPress={() => void submitWaitReport()}>
            <Text>Queue wait report</Text>
          </Button>
        </CardContent>
      </Card>

      <PromptDialog
        visible={waitPromptOpen}
        mode="number"
        min={0}
        max={1440}
        title="How long ago did the wait start?"
        message="Minutes before now. Used as the observed start of the wait."
        unitLabel="min"
        submitLabel="Set start"
        onCancel={() => setWaitPromptOpen(false)}
        onSubmit={(value) => {
          const minutes = typeof value === 'number' ? value : Number(value);
          if (Number.isFinite(minutes)) {
            setWaitStart(new Date(Date.now() - minutes * 60_000).toISOString());
          }
          setWaitPromptOpen(false);
        }}
      />
    </ScrollView>
  );
}
