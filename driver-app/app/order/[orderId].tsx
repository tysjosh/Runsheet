import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Stack, useLocalSearchParams, useRouter } from 'expo-router';
import { ScrollView, View } from 'react-native';

import { Button } from '@/components/ui/button';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { Text } from '@/components/ui/text';
import { queryKeys, WORK_SCOPE } from '@/lib/query-keys';
import {
  buildCompartmentLedger,
  crossContaminationMessage,
} from '@/lib/route-api';
import { formatGallons } from '@/lib/units';
import { loadWorkDetail, queueOrderStatus } from '@/lib/work-api';

export default function DeliveryDetailScreen() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const params = useLocalSearchParams<{ orderId: string }>();
  const orderId = Array.isArray(params.orderId)
    ? params.orderId[0]
    : params.orderId;
  const detail = useQuery({
    queryKey: queryKeys.order(orderId || ''),
    queryFn: () => loadWorkDetail(orderId || ''),
    enabled: Boolean(orderId),
  });
  const start = useMutation({
    mutationFn: () => queueOrderStatus(orderId || '', 'in_transit'),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: [WORK_SCOPE] });
      await detail.refetch();
    },
  });

  const order = detail.data;
  const ledger = buildCompartmentLedger(order);
  const message = start.isError
    ? start.error instanceof Error
      ? start.error.message
      : 'The delivery could not be started.'
    : start.data && !start.data.synced
      ? 'Start saved on this device. It will sync when service returns.'
      : null;

  return (
    <>
      <Stack.Screen options={{ title: order?.customer_name || 'Delivery' }} />
      <ScrollView
        className="flex-1 bg-background"
        contentContainerClassName="gap-5 p-5 pb-12"
      >
        {detail.isLoading && <Text>Loading delivery…</Text>}
        {detail.isError && (
          <Card className="border-red-300">
            <CardHeader>
              <CardTitle>Delivery unavailable</CardTitle>
              <CardDescription>
                Confirm the assignment and try again.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <Button onPress={() => void detail.refetch()}>
                <Text>Retry</Text>
              </Button>
            </CardContent>
          </Card>
        )}

        {order && (
          <>
            <Card>
              <CardHeader>
                <CardTitle>{order.customer_name}</CardTitle>
                <CardDescription>{order.destination.address}</CardDescription>
              </CardHeader>
              <CardContent className="gap-2">
                <Text className="font-semibold">
                  {order.product_grade} · {formatGallons(order.ordered_gallons)}
                </Text>
                <Text>
                  Delivery window:{' '}
                  {new Date(order.delivery_window_start).toLocaleString()} –{' '}
                  {new Date(order.delivery_window_end).toLocaleString()}
                </Text>
                {order.customer_phone && <Text>{order.customer_phone}</Text>}
              </CardContent>
            </Card>

            <View className="gap-3">
              <Text className="text-xl font-bold">Load manifest</Text>
              {order.manifest_available && ledger.length > 0 ? (
                ledger.map((row) => (
                  <Card key={row.compartmentId}>
                    <CardContent className="gap-1 p-4">
                      <Text className="font-semibold">
                        Compartment {row.compartmentId}
                      </Text>
                      <Text>
                        {row.loadedGrade} · {formatGallons(row.loadedGallons)}{' '}
                        loaded · {formatGallons(row.remainingGallons)} remaining
                      </Text>
                      {crossContaminationMessage(row) && (
                        <Text className="font-semibold text-destructive">
                          {crossContaminationMessage(row)}
                        </Text>
                      )}
                    </CardContent>
                  </Card>
                ))
              ) : (
                <Text className="text-muted-foreground">
                  No compartment manifest is available for this assignment.
                </Text>
              )}
            </View>

            <View className="gap-3">
              <Text className="text-xl font-bold">Stop sequence</Text>
              {order.route_available && (order.stops?.length ?? 0) > 0 ? (
                order.stops?.map((stop) => (
                  <Card key={`${stop.sequence}-${stop.station_id}`}>
                    <CardContent className="gap-1 p-4">
                      <Text className="font-semibold">
                        {stop.sequence + 1}. Stop {stop.station_id}
                      </Text>
                      <Text>
                        ETA:{' '}
                        {stop.planned_arrival
                          ? new Date(stop.planned_arrival).toLocaleString()
                          : 'Not available'}
                      </Text>
                      {Object.entries(stop.planned_gallons_by_grade).map(
                        ([grade, gallons]) => (
                          <Text key={grade}>
                            {grade}: {formatGallons(gallons)}
                          </Text>
                        ),
                      )}
                    </CardContent>
                  </Card>
                ))
              ) : (
                <Text className="text-muted-foreground">
                  No route sequence is available for this assignment.
                </Text>
              )}
            </View>

            {message && <Text className="text-sm">{message}</Text>}

            {order.status === 'dispatched' ? (
              <Button
                disabled={start.isPending}
                onPress={() => start.mutate()}
              >
                <Text>
                  {start.isPending ? 'Starting delivery…' : 'Start delivery'}
                </Text>
              </Button>
            ) : (
              <Button
                onPress={() =>
                  router.push({
                    pathname: '/order/[orderId]/pod',
                    params: { orderId: order.order_id },
                  })
                }
              >
                <Text>Capture POD and actual gallons</Text>
              </Button>
            )}

            <Button
              variant="outline"
              onPress={() =>
                router.push({
                  pathname: '/order/[orderId]/exception',
                  params: { orderId: order.order_id },
                })
              }
            >
              <Text>Report a problem</Text>
            </Button>
          </>
        )}
      </ScrollView>
    </>
  );
}
