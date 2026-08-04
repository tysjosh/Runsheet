import { useNetInfo } from '@react-native-community/netinfo';
import { useQuery } from '@tanstack/react-query';
import { useRouter } from 'expo-router';
import { useEffect, useState } from 'react';
import { RefreshControl, ScrollView, View } from 'react-native';

import { PendingQueueChip } from '@/components/PendingQueueChip';
import { Button } from '@/components/ui/button';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { Text } from '@/components/ui/text';
import {
  drainQueue,
  queueDepth,
  subscribeToQueue,
  type QueueDepth,
} from '@/lib/offline-queue';
import { syncPendingPodCaptures } from '@/lib/pod-api';
import { queryKeys } from '@/lib/query-keys';
import { formatGallons } from '@/lib/units';
import { loadAssignedWork } from '@/lib/work-api';

const EMPTY_DEPTH: QueueDepth = {
  pending: 0,
  inFlight: 0,
  failed: 0,
  conflict: 0,
  outstanding: 0,
};

export default function AssignedWorkScreen() {
  const router = useRouter();
  const network = useNetInfo();
  const [depth, setDepth] = useState<QueueDepth>(EMPTY_DEPTH);
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
    const latest = await queueDepth();
    setDepth(latest);
  };

  const orders = work.data?.data ?? [];
  const isOnline = network.isConnected !== false;

  return (
    <ScrollView
      className="flex-1 bg-background"
      contentContainerClassName="gap-5 p-5 pb-28"
      refreshControl={
        <RefreshControl
          refreshing={work.isRefetching}
          onRefresh={() => void refresh()}
        />
      }
    >
      <View className="flex-row items-start justify-between gap-3">
        <View className="flex-1 gap-1">
          <Text className="text-3xl font-bold">Assigned work</Text>
          <Text className="text-muted-foreground">
            Deliveries dispatched to this driver
          </Text>
        </View>
        <PendingQueueChip counts={depth} isOnline={isOnline} />
      </View>

      {work.isLoading && (
        <Card>
          <CardContent className="p-6">
            <Text>Loading assigned deliveries…</Text>
          </CardContent>
        </Card>
      )}

      {work.isError && (
        <Card className="border-red-300">
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
            <CardDescription>
              Pull down to check for newly dispatched work.
            </CardDescription>
          </CardHeader>
        </Card>
      )}

      {orders.map((order) => (
        <Card key={order.order_id}>
          <CardHeader>
            <View className="flex-row items-start justify-between gap-3">
              <View className="flex-1 gap-1">
                <CardTitle>{order.customer_name}</CardTitle>
                <CardDescription>{order.destination.address}</CardDescription>
              </View>
              <View className="rounded-full bg-primary/10 px-3 py-1">
                <Text className="text-xs font-semibold uppercase text-primary">
                  {order.status.replace('_', ' ')}
                </Text>
              </View>
            </View>
          </CardHeader>
          <CardContent className="gap-4">
            <View className="rounded-xl bg-muted p-4">
              <Text className="font-semibold">
                {order.product_grade} · {formatGallons(order.ordered_gallons)}
              </Text>
              <Text className="mt-1 text-sm text-muted-foreground">
                Window: {new Date(order.delivery_window_start).toLocaleString()}
              </Text>
            </View>

            <Button
              onPress={() =>
                router.push({
                  pathname: '/order/[orderId]',
                  params: { orderId: order.order_id },
                })
              }
            >
              <Text>
                {order.status === 'in_transit'
                  ? 'Continue delivery'
                  : 'Review and start'}
              </Text>
            </Button>
          </CardContent>
        </Card>
      ))}
    </ScrollView>
  );
}
