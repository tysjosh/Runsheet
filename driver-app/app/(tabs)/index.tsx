/**
 * Work — today's deliveries (UI revamp task 4.3, R13.3, D16).
 *
 * One large title ("Today"), the duty-status chip moved here from Profile, the
 * offline queue chip, then the next stop as a large card with Navigate, Call
 * and Arrive, then the remaining stops as compact rows. Data and refresh are
 * unchanged: the same work query, the same POD sync and queue drain on pull.
 */

import { useNetInfo } from '@react-native-community/netinfo';
import { useQuery } from '@tanstack/react-query';
import { useEffect, useMemo, useState } from 'react';
import { RefreshControl, ScrollView, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import {
  DutyStatusChip,
  DutyStatusSheet,
  HosLimitBanner,
  StartShiftCard,
  useDutyStatus,
} from '@/components/DutyStatusControl';
import { PendingQueueChip } from '@/components/PendingQueueChip';
import { NextStopCard, StopRow } from '@/components/WorkStops';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Text } from '@/components/ui/text';
import { date } from '@/lib/format';
import { drainQueue, queueDepth, subscribeToQueue, type QueueDepth } from '@/lib/offline-queue';
import { syncPendingPodCaptures } from '@/lib/pod-api';
import { queryKeys } from '@/lib/query-keys';
import { loadAssignedWork } from '@/lib/work-api';
import { sortWork } from '@/lib/work-view';

const EMPTY_DEPTH: QueueDepth = {
  pending: 0,
  inFlight: 0,
  failed: 0,
  conflict: 0,
  outstanding: 0,
};

export default function AssignedWorkScreen() {
  const network = useNetInfo();
  const insets = useSafeAreaInsets();
  const [depth, setDepth] = useState<QueueDepth>(EMPTY_DEPTH);
  const [dutyOpen, setDutyOpen] = useState(false);
  const duty = useDutyStatus({ onLeave: () => setDutyOpen(false) });
  const work = useQuery({
    queryKey: queryKeys.work({
      statuses: ['dispatched', 'in_transit'],
      size: 50,
    }),
    queryFn: loadAssignedWork,
  });

  useEffect(() => {
    void queueDepth().then(setDepth).catch(() => undefined);
    return subscribeToQueue(setDepth);
  }, []);

  const refresh = async () => {
    await syncPendingPodCaptures();
    await drainQueue();
    await work.refetch();
    void duty.refetch();
    const latest = await queueDepth();
    setDepth(latest);
  };

  const orders = useMemo(() => sortWork(work.data?.data ?? []), [work.data]);
  const [next, ...rest] = orders;
  const isOnline = network.isConnected !== false;

  return (
    <ScrollView
      className="flex-1 bg-background"
      contentContainerClassName="gap-4 p-5 pb-28"
      // Work has no stack header (its title is the large "Today"), so it keeps
      // clear of the status bar itself.
      contentContainerStyle={{ paddingTop: insets.top + 20 }}
      refreshControl={
        <RefreshControl refreshing={work.isRefetching} onRefresh={() => void refresh()} />
      }
    >
      <View className="flex-row items-center justify-between gap-3">
        <View className="flex-1">
          <Text role="heading" aria-level={1} className="text-3xl font-bold">
            Today
          </Text>
          <Text className="text-muted-foreground">{date(new Date())}</Text>
        </View>
        <DutyStatusChip duty={duty} onPress={() => setDutyOpen(true)} />
      </View>

      <PendingQueueChip counts={depth} isOnline={isOnline} className="self-start" />

      <HosLimitBanner />
      <StartShiftCard duty={duty} />

      {work.isLoading && (
        <Card>
          <CardContent className="p-6">
            <Text>Loading assigned deliveries…</Text>
          </CardContent>
        </Card>
      )}

      {work.isError && (
        <Card className="border-destructive">
          <CardHeader>
            <CardTitle>Work list unavailable</CardTitle>
            <CardDescription>
              Check the driver session and network connection, then try again.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Button onPress={() => void work.refetch()}>
              <Text>Retry</Text>
            </Button>
          </CardContent>
        </Card>
      )}

      {!work.isLoading && !work.isError && orders.length === 0 && (
        <Card>
          <CardHeader>
            <CardTitle>No active deliveries</CardTitle>
            <CardDescription>Pull down to check for newly dispatched work.</CardDescription>
          </CardHeader>
        </Card>
      )}

      {next && <NextStopCard order={next} />}

      {rest.length > 0 && (
        <View className="gap-2">
          <Text role="heading" aria-level={2} className="text-lg font-semibold">
            Later today · {rest.length}
          </Text>
          {rest.map((order) => (
            <StopRow key={order.order_id} order={order} />
          ))}
        </View>
      )}

      <DutyStatusSheet duty={duty} visible={dutyOpen} onClose={() => setDutyOpen(false)} />
    </ScrollView>
  );
}
