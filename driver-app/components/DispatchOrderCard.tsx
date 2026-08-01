/**
 * Copied from azumi-rider/components/DispatchOrderCard.tsx
 * Copied: 2026-07-29
 * Donor: azumi-rider (Expo SDK 53). Retains the countdown offer-card pattern
 * (Requirements 16.2, 16.3, 16.5): the expiry resolved from the server's
 * `expiresAt` with the server's `timeoutSeconds` as the fallback — never a
 * hard-coded duration — the one-second tick, the `expired` latch that fires
 * `onExpire` exactly once, the colour-graded timer badge, the expand/collapse
 * body, the accept button with its in-flight spinner, and the decline sheet.
 *
 * Changed: the donor's two `RiderOrder` shapes become the single `FuelOrder`
 * from `types/order.ts` (Requirement 16.14); `formatNaira` becomes
 * `formatCurrency` from `lib/units.ts`, which defaults to US dollars
 * (Requirement 16.9); volumes render as `gal` and distances as `mi` through the
 * same module (Requirements 16.10, 16.19). Dropped with the donor domain: the
 * shop, menu-item, and option-group entities (Requirement 16.8), the rider
 * earnings and estimated-earnings breakdown (Requirement 16.6), the
 * `~/lib/dispatch-mutations` `/rider/*` clients (Requirement 16.12) — accept and
 * decline are callbacks the caller wires to the REST endpoints — and the
 * `riderId` / `riderUserId` props, since the server derives the driver from the
 * session.
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Modal, Pressable, View } from 'react-native';
import { ChevronDown, ChevronUp, Clock, MoreVertical, Package } from 'lucide-react-native';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Spinner } from '@/components/ui/spinner';
import { Text } from '@/components/ui/text';
import { formatCurrency, formatGallons, formatMiles } from '@/lib/units';
import type { FuelOrder } from '@/types/order';

/**
 * A dispatch offer: the order itself plus the offer envelope that carries it.
 *
 * The countdown duration is always the server's, per the donor's rule that the
 * timeout must not be hard-coded on the device.
 */
export interface OrderOffer {
  /** The single order type — there is no separate offer view type (R16.14). */
  order: FuelOrder;
  /** Server-supplied offer window, in seconds. Fallback when `expiresAt` is absent. */
  timeoutSeconds: number;
  /** ISO-8601 instant the offer lapses. Preferred over `timeoutSeconds`. */
  expiresAt?: string;
  /** Which dispatch attempt this offer is, when the server reports it. */
  attemptNumber?: number;
  /** True when the offer went to several drivers at once. */
  isBroadcast?: boolean;
  /** Driving distance to the destination, in miles (R16.10). */
  distanceMiles?: number;
  /** Order value in US dollars (R16.9). Never driver earnings (R16.6). */
  estimatedValueUsd?: number;
}

export interface DispatchOrderCardProps {
  offer: OrderOffer;
  onExpire?: (orderId: string) => void;
  onAccept?: (orderId: string) => void;
  onDecline?: (orderId: string, reason: DeclineReason) => void;
  isAccepting?: boolean;
  isDeclining?: boolean;
}

/** Decline reasons a fuel driver can give. No payment or earnings reason (R16.6). */
export type DeclineReason =
  | 'TOO_FAR'
  | 'HOS_LIMIT'
  | 'VEHICLE_ISSUE'
  | 'GOING_OFF_DUTY'
  | 'WRONG_PRODUCT'
  | 'OTHER';

const DECLINE_REASONS: { value: DeclineReason; label: string }[] = [
  { value: 'TOO_FAR', label: 'Too far away' },
  { value: 'HOS_LIMIT', label: 'Not enough drive time left' },
  { value: 'VEHICLE_ISSUE', label: 'Truck or trailer issue' },
  { value: 'GOING_OFF_DUTY', label: 'Going off duty' },
  { value: 'WRONG_PRODUCT', label: 'Cannot carry this product' },
  { value: 'OTHER', label: 'Other reason' },
];

/**
 * Seconds left on an offer. `expiresAt` wins when present; otherwise the
 * server's `timeoutSeconds` is used. Never negative.
 */
export function secondsRemainingOn(offer: OrderOffer, now: number = Date.now()): number {
  if (offer.expiresAt) {
    const expiryTime = new Date(offer.expiresAt).getTime();
    if (Number.isFinite(expiryTime)) {
      return Math.max(0, Math.floor((expiryTime - now) / 1000));
    }
  }
  return Math.max(0, offer.timeoutSeconds);
}

/** `45s`, or `1m 30s` past a minute. Retained from the donor verbatim. */
export function formatTimeRemaining(seconds: number): string {
  if (seconds < 60) {
    return `${seconds}s`;
  }
  const minutes = Math.floor(seconds / 60);
  const secs = seconds % 60;
  return `${minutes}m ${secs}s`;
}

/** Timer badge colour: green above half the window, amber above a quarter, else red. */
export function timerToneClass(secondsRemaining: number, timeoutSeconds: number): string {
  const percentage = timeoutSeconds > 0 ? (secondsRemaining / timeoutSeconds) * 100 : 0;
  if (percentage > 50) return 'bg-green-500';
  if (percentage > 25) return 'bg-yellow-500';
  return 'bg-red-500';
}

export const DispatchOrderCard = React.memo(function DispatchOrderCard({
  offer,
  onExpire,
  onAccept,
  onDecline,
  isAccepting,
  isDeclining,
}: DispatchOrderCardProps) {
  const { order } = offer;

  const [expanded, setExpanded] = useState(false);
  const [showDeclineSheet, setShowDeclineSheet] = useState(false);
  const [secondsRemaining, setSecondsRemaining] = useState(() => secondsRemainingOn(offer));
  const [isExpired, setIsExpired] = useState(false);

  // Countdown. Latches on expiry so `onExpire` fires exactly once.
  useEffect(() => {
    if (isExpired) {
      return;
    }
    if (secondsRemaining <= 0) {
      setIsExpired(true);
      onExpire?.(order.order_id);
      return;
    }

    const interval = setInterval(() => {
      setSecondsRemaining((prev) => Math.max(0, prev - 1));
    }, 1000);

    return () => clearInterval(interval);
  }, [secondsRemaining, isExpired, order.order_id, onExpire]);

  const handleAccept = useCallback(() => {
    if (isExpired) {
      return;
    }
    onAccept?.(order.order_id);
  }, [isExpired, onAccept, order.order_id]);

  const handleDecline = useCallback(
    (reason: DeclineReason) => {
      setShowDeclineSheet(false);
      onDecline?.(order.order_id, reason);
    },
    [onDecline, order.order_id]
  );

  const timerBadge = useMemo(
    () => (
      <View
        className={`px-3 py-1.5 rounded-full ${timerToneClass(
          secondsRemaining,
          offer.timeoutSeconds
        )} ${isExpired ? 'opacity-50' : ''}`}
      >
        <Text className="text-xs font-semibold text-white">
          {isExpired ? 'Expired' : formatTimeRemaining(secondsRemaining)}
        </Text>
      </View>
    ),
    [secondsRemaining, isExpired, offer.timeoutSeconds]
  );

  const dispatchBadge = useMemo(() => {
    if (offer.isBroadcast) {
      return (
        <View className="px-3 py-1.5 rounded-full bg-red-50 border border-red-200 flex-row items-center">
          <Text className="text-xs font-semibold text-red-700">BROADCAST</Text>
        </View>
      );
    }
    return (
      <View className="px-3 py-1.5 rounded-full bg-primary/10 border border-primary/20 flex-row items-center">
        <Text className="text-xs font-semibold text-primary">New</Text>
      </View>
    );
  }, [offer.isBroadcast]);

  const deliveryWindow = `${new Date(order.delivery_window_start).toLocaleString()} – ${new Date(
    order.delivery_window_end
  ).toLocaleString()}`;

  return (
    <>
      <Card className="mb-6 shadow-sm">
        <CardContent className="p-6">
          <Pressable
            accessibilityRole="button"
            accessibilityLabel={expanded ? 'Collapse offer details' : 'Expand offer details'}
            onPress={() => setExpanded(!expanded)}
          >
            {/* Header */}
            <View className="flex-row justify-between items-center">
              <Text className="text-lg font-bold text-foreground">{order.order_id}</Text>
              <View className="flex-row items-center gap-2">
                {dispatchBadge}
                {timerBadge}
                {expanded ? (
                  <ChevronUp size={20} color="#6b7280" />
                ) : (
                  <ChevronDown size={20} color="#6b7280" />
                )}
              </View>
            </View>

            {/* Load summary */}
            <View className="h-px bg-border my-4 rounded-sm" />
            <View className="flex-row items-center mb-1">
              <Package size={20} color="#6b7280" />
              <Text className="text-sm font-medium text-muted-foreground ml-2">
                {order.product_grade} · {formatGallons(order.ordered_gallons)}
              </Text>
            </View>
            <Text className="text-sm text-muted-foreground mt-2">
              {order.destination.address}
              {offer.distanceMiles !== undefined && ` • ${formatMiles(offer.distanceMiles)}`}
              {offer.estimatedValueUsd !== undefined &&
                ` • ${formatCurrency(offer.estimatedValueUsd)}`}
            </Text>

            {/* Timer warning */}
            {!isExpired && secondsRemaining < 30 && (
              <View className="bg-red-50 border border-red-200 rounded-lg p-3 mt-4 flex-row items-center">
                <Clock size={16} color="#ef4444" />
                <Text className="text-sm font-medium text-red-600 ml-2">
                  {formatTimeRemaining(secondsRemaining)} remaining
                </Text>
              </View>
            )}

            {/* Expanded detail */}
            {expanded && (
              <>
                <View className="h-px bg-border my-4 rounded-sm" />

                <View className="mb-4">
                  <Text className="text-sm font-semibold text-foreground mb-1">Deliver to</Text>
                  <Text className="text-sm text-muted-foreground">{order.customer_name}</Text>
                  <Text className="text-sm text-muted-foreground">
                    {order.destination.address}
                  </Text>
                </View>

                <View className="mb-4">
                  <Text className="text-sm font-semibold text-foreground mb-1">
                    Delivery window
                  </Text>
                  <Text className="text-sm text-muted-foreground">{deliveryWindow}</Text>
                </View>

                <View className="bg-muted/30 rounded-lg p-4">
                  <View className="flex-row justify-between mb-2">
                    <Text className="text-sm text-muted-foreground">Product</Text>
                    <Text className="text-sm font-semibold text-foreground">
                      {order.product_grade}
                    </Text>
                  </View>
                  <View className="flex-row justify-between mb-2">
                    <Text className="text-sm text-muted-foreground">Volume</Text>
                    <Text className="text-sm font-semibold text-foreground">
                      {formatGallons(order.ordered_gallons)}
                    </Text>
                  </View>
                  {offer.distanceMiles !== undefined && (
                    <View className="flex-row justify-between mb-2">
                      <Text className="text-sm text-muted-foreground">Distance</Text>
                      <Text className="text-sm font-semibold text-foreground">
                        {formatMiles(offer.distanceMiles)}
                      </Text>
                    </View>
                  )}
                  {offer.attemptNumber !== undefined && (
                    <View className="flex-row justify-between">
                      <Text className="text-sm text-muted-foreground">Offer attempt</Text>
                      <Text className="text-sm font-semibold text-foreground">
                        {offer.attemptNumber}
                      </Text>
                    </View>
                  )}
                </View>
              </>
            )}
          </Pressable>

          {/* Actions */}
          <View className="flex-row mt-6 gap-3 items-stretch">
            <Button
              size="lg"
              className="flex-[9] rounded-xl"
              onPress={handleAccept}
              disabled={isAccepting || isDeclining || isExpired}
            >
              <View className="flex-row items-center justify-center gap-2">
                {isAccepting && (
                  <Spinner
                    size={22}
                    strokeWidth={4}
                    color="#ffffff"
                    trackColor="rgba(255,255,255,0.2)"
                  />
                )}
                <Text className="text-primary-foreground text-lg font-semibold">
                  {isAccepting ? 'Accepting…' : isExpired ? 'Offer expired' : 'Accept order'}
                </Text>
              </View>
            </Button>

            <Pressable
              accessibilityRole="button"
              accessibilityLabel="Decline order"
              className="flex-[1] px-2 rounded-xl border border-border bg-background h-16 items-center justify-center"
              onPress={() => setShowDeclineSheet(true)}
              disabled={isAccepting || isDeclining || isExpired}
            >
              <MoreVertical size={20} color="#6b7280" />
            </Pressable>
          </View>
        </CardContent>
      </Card>

      {/* Decline sheet */}
      <Modal
        visible={showDeclineSheet}
        transparent
        animationType="slide"
        onRequestClose={() => setShowDeclineSheet(false)}
      >
        <Pressable
          accessibilityRole="button"
          accessibilityLabel="Dismiss decline options"
          className="flex-1 bg-black/50 justify-end"
          onPress={() => setShowDeclineSheet(false)}
        >
          <Pressable onPress={(e) => e.stopPropagation()} className="bg-background rounded-t-3xl">
            <View className="items-center pt-3 pb-2">
              <View className="w-10 h-1 rounded-full bg-border" />
            </View>

            <View className="px-5 pb-8 pt-4">
              <Text className="text-2xl font-bold text-foreground mb-1">Decline order</Text>
              <Text className="text-sm text-muted-foreground mb-6">
                Why are you declining this delivery?
              </Text>

              <View className="gap-2 mb-6">
                {DECLINE_REASONS.map((reason) => (
                  <Pressable
                    key={reason.value}
                    accessibilityRole="button"
                    onPress={() => handleDecline(reason.value)}
                    disabled={isDeclining}
                    className="flex-row items-center p-4 rounded-xl border border-border bg-background active:bg-muted/50"
                  >
                    <Text className="text-base font-medium text-foreground flex-1">
                      {reason.label}
                    </Text>
                  </Pressable>
                ))}
              </View>

              <Button variant="ghost" onPress={() => setShowDeclineSheet(false)} className="w-full">
                <Text className="text-base font-medium text-muted-foreground">Cancel</Text>
              </Button>
            </View>
          </Pressable>
        </Pressable>
      </Modal>
    </>
  );
});
