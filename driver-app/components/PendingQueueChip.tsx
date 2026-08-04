/**
 * PendingQueueChip — NEW. The count of queued mutations the driver can see
 * while the device has no usable connection (R11.10), and the count of failed
 * ones the driver has to act on (R11.14).
 *
 * The chip takes its counts as props rather than reading `lib/offline-queue.ts`
 * itself: the queue's own subscription (`subscribeToQueue`) is the source, the
 * chip is the display. {@link PendingQueueCounts} is a structural subset of the
 * queue's `QueueDepth`, so a caller can pass a depth straight through, and this
 * file imports nothing from the queue module.
 *
 * Nothing here is copied or adapted from the donor (R16.21).
 *
 * Requirements: 11.10, 16.15 (the failed-count tap target replaces no donor
 * surface — the donor has no queue), 16.21
 */

import React from 'react';
import { Pressable, View } from 'react-native';
import { CloudUpload, WifiOff } from 'lucide-react-native';
import { Badge } from '@/components/ui/badge';
import { Text } from '@/components/ui/text';
import { cn } from '@/lib/utils';

/**
 * What the chip needs from the offline queue.
 *
 * A structural subset of `QueueDepth` in `lib/offline-queue.ts`: `pending` is
 * everything still waiting to be sent, `failed` is everything that stopped
 * retrying and needs the driver (R11.14). `in_flight` and `conflict` rows are
 * deliberately not counted here — an in-flight row is already on its way, and a
 * conflict has its own driver-visible entry (R11.13).
 */
export interface PendingQueueCounts {
  pending: number;
  failed: number;
}

function safeCount(value: number): number {
  return Number.isFinite(value) && value > 0 ? Math.floor(value) : 0;
}

/** `pending + failed` — the number R11.10 puts in front of the driver. */
export function outstandingQueueCount(counts: PendingQueueCounts): number {
  return safeCount(counts.pending) + safeCount(counts.failed);
}

/**
 * Show the chip whenever connectivity is unavailable — so the driver knows work
 * is being held rather than lost — or whenever anything is outstanding, even
 * online, because a failed row does not clear itself.
 */
export function shouldShowQueueChip(counts: PendingQueueCounts, isOnline: boolean): boolean {
  return !isOnline || outstandingQueueCount(counts) > 0;
}

/** The chip's text. `Offline` is stated first, because it explains the count. */
export function queueChipLabel(counts: PendingQueueCounts, isOnline: boolean): string {
  const outstanding = outstandingQueueCount(counts);
  const failed = safeCount(counts.failed);
  const prefix = isOnline ? '' : 'Offline · ';

  if (outstanding === 0) {
    return `${prefix}nothing queued`;
  }
  const core = `${outstanding} queued`;
  return failed > 0 ? `${prefix}${core} · ${failed} failed` : `${prefix}${core}`;
}

/** Failed rows are the driver's problem, so they outrank the offline styling. */
export function queueChipVariant(
  counts: PendingQueueCounts,
  isOnline: boolean,
): 'default' | 'secondary' | 'destructive' {
  if (safeCount(counts.failed) > 0) {
    return 'destructive';
  }
  return isOnline ? 'default' : 'secondary';
}

export interface PendingQueueChipProps {
  counts: PendingQueueCounts;
  /** Connectivity, from the same NetInfo signal the queue drains on. */
  isOnline: boolean;
  /** Opens the queue detail — the failed list and the conflict entries. */
  onPress?: () => void;
  className?: string;
}

export function PendingQueueChip({
  counts,
  isOnline,
  onPress,
  className,
}: PendingQueueChipProps) {
  if (!shouldShowQueueChip(counts, isOnline)) {
    return null;
  }

  const label = queueChipLabel(counts, isOnline);
  const variant = queueChipVariant(counts, isOnline);
  const Icon = isOnline ? CloudUpload : WifiOff;

  const chip = (
    <Badge variant={variant} className={cn('flex-row items-center gap-1.5 px-3 py-1', className)}>
      <Icon size={12} color={variant === 'secondary' ? '#111827' : '#ffffff'} />
      <Text>{label}</Text>
    </Badge>
  );

  if (!onPress) {
    return (
      <View accessibilityRole="text" accessibilityLabel={label}>
        {chip}
      </View>
    );
  }

  return (
    <Pressable
      onPress={onPress}
      accessibilityRole="button"
      accessibilityLabel={`${label}. Open the queue.`}
    >
      {chip}
    </Pressable>
  );
}
