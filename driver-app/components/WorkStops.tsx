/**
 * The Work screen's next-stop card and compact remaining-stop rows (UI revamp
 * R13.3, R13.4, design.md §8). Large type and three 56 pt actions, nothing that
 * needs reading while the truck is moving.
 *
 * - Navigate hands the address to the platform maps app (`lib/handoff.ts`).
 * - Call opens `tel:` (hidden when the session has no PII access, R15.6).
 * - Arrive opens the existing stop check-in on the Route tab, prefilled with
 *   that stop's planned gallons; the check-in itself (gate, geotag, queue) is
 *   unchanged.
 */

import { useQuery } from '@tanstack/react-query';
import { useRouter } from 'expo-router';
import { ChevronRight, MapPin } from 'lucide-react-native';
import { Pressable, View } from 'react-native';

import { ProductCap, ProductChip } from '@/components/ProductChip';
import { StatusBadge, statusKeyForOrder } from '@/components/StatusBadge';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Text } from '@/components/ui/text';
import { useColorScheme } from '@/hooks/useColorScheme';
import { timeWindow } from '@/lib/format';
import { mapsUrl, openHandoff, telUrl } from '@/lib/handoff';
import { queryKeys } from '@/lib/query-keys';
import { paletteFor } from '@/lib/theme';
import { formatGallons } from '@/lib/units';
import { loadWorkDetail } from '@/lib/work-api';
import { nextPendingStop, productLines } from '@/lib/work-view';
import type { FuelOrder } from '@/types/order';

export function NextStopCard({ order }: { order: FuelOrder }) {
  const router = useRouter();
  const palette = paletteFor(useColorScheme());
  // The list item carries no manifest; the detail (same query key as the
  // delivery screen, so it is shared) adds the compartments and stops.
  const detail = useQuery({
    queryKey: queryKeys.order(order.order_id),
    queryFn: () => loadWorkDetail(order.order_id),
  });
  const full = detail.data ?? order;
  const lines = productLines(full);
  const navigateUrl = mapsUrl(order.destination);
  const callUrl = telUrl(full.customer_phone ?? order.customer_phone);
  const stop = nextPendingStop(full);

  const openDetail = () =>
    router.push({ pathname: '/order/[orderId]', params: { orderId: order.order_id } });

  const arrive = () =>
    router.push({
      pathname: '/route',
      params: {
        orderId: order.order_id,
        ...(stop ? { checkin: String(stop.sequence) } : {}),
      },
    });

  return (
    <Card testID="next-stop" className="border-2 border-primary">
      <CardContent className="gap-4 p-5">
        <View className="flex-row items-center justify-between gap-2">
          <Text className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
            Next stop
          </Text>
          <StatusBadge status={statusKeyForOrder(order.status)} />
        </View>

        <Pressable
          accessibilityRole="button"
          accessibilityHint="Opens the delivery"
          onPress={openDetail}
          className="gap-1"
        >
          <Text role="heading" aria-level={2} className="text-2xl font-bold">
            {order.customer_name}
          </Text>
          <View className="flex-row items-start gap-2">
            <MapPin size={18} color={palette.textMuted} style={{ marginTop: 3 }} />
            <Text className="flex-1 text-lg text-foreground">{order.destination.address}</Text>
          </View>
          <Text className="text-lg font-semibold">
            Window {timeWindow(order.delivery_window_start, order.delivery_window_end)}
          </Text>
        </Pressable>

        {lines.length > 0 && (
          <View className="gap-2 rounded-xl bg-muted p-3">
            {lines.map((line) => (
              <ProductChip key={line.grade} code={line.grade} suffix={formatGallons(line.gallons, { maximumFractionDigits: 0 })} />
            ))}
          </View>
        )}

        <View className="flex-row gap-2">
          <Button
            className="flex-1 px-2"
            size="default"
            disabled={!navigateUrl}
            accessibilityLabel={`Navigate to ${order.destination.address}`}
            onPress={() => void openHandoff(navigateUrl)}
          >
            <Text>Navigate</Text>
          </Button>
          {callUrl && (
            <Button
              className="flex-1 px-2"
              size="default"
              variant="outline"
              accessibilityLabel={`Call ${order.customer_name}`}
              onPress={() => void openHandoff(callUrl)}
            >
              <Text>Call</Text>
            </Button>
          )}
          <Button
            className="flex-1 px-2"
            size="default"
            variant="outline"
            accessibilityLabel={`Arrive at ${order.customer_name}: check in`}
            onPress={arrive}
          >
            <Text>Arrive</Text>
          </Button>
        </View>

        <Button variant="secondary" size="default" onPress={openDetail}>
          <Text>{order.status === 'in_transit' ? 'Continue delivery' : 'Review and start'}</Text>
        </Button>
      </CardContent>
    </Card>
  );
}

/** One 64 pt row per remaining stop: window, customer, product caps. */
export function StopRow({ order }: { order: FuelOrder }) {
  const router = useRouter();
  const palette = paletteFor(useColorScheme());
  const lines = productLines(order);
  const windowText = timeWindow(order.delivery_window_start, order.delivery_window_end);
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={`${order.customer_name}, window ${windowText}, ${order.destination.address}`}
      onPress={() => router.push({ pathname: '/order/[orderId]', params: { orderId: order.order_id } })}
      className="min-h-16 flex-row items-center gap-3 rounded-xl border-2 border-input bg-card px-4 py-2"
      testID="stop-row"
    >
      <View className="flex-row gap-1">
        {lines.map((line) => (
          <ProductCap key={line.grade} code={line.grade} size={28} />
        ))}
      </View>
      <View className="flex-1">
        <Text className="font-semibold" numberOfLines={1}>
          {order.customer_name}
        </Text>
        <Text className="text-sm text-muted-foreground" numberOfLines={1}>
          {windowText} · {order.destination.address}
        </Text>
      </View>
      <ChevronRight size={22} color={palette.textMuted} />
    </Pressable>
  );
}
