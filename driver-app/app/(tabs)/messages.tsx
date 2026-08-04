/**
 * Dispatch thread.
 *
 * Messaging is keyed on the unit of work, so the screen is an order picker over
 * the assigned-work list plus one thread. The sender identity is never chosen
 * here — `lib/messages-api.ts` fills it from the verified session, which is the
 * same value the server derives (R7.5).
 *
 * Requirements: 7.5, 7.12, 7.14, 16.13
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useMemo, useState } from 'react';
import {
  KeyboardAvoidingView,
  Platform,
  Pressable,
  RefreshControl,
  ScrollView,
  View,
} from 'react-native';

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
import { loadThread, sendThreadMessage } from '@/lib/messages-api';
import { queryKeys } from '@/lib/query-keys';
import { loadAssignedWork } from '@/lib/work-api';

function timeOf(timestamp: string): string {
  const parsed = Date.parse(timestamp);
  return Number.isFinite(parsed)
    ? new Date(parsed).toLocaleString()
    : timestamp;
}

export default function MessagesScreen() {
  const queryClient = useQueryClient();
  const [selectedOrderId, setSelectedOrderId] = useState<string | null>(null);
  const [draft, setDraft] = useState('');
  const [message, setMessage] = useState<string | null>(null);

  const work = useQuery({
    queryKey: queryKeys.work({
      statuses: ['dispatched', 'in_transit'],
      size: 50,
    }),
    queryFn: loadAssignedWork,
  });

  const orders = useMemo(() => work.data?.data ?? [], [work.data]);

  useEffect(() => {
    if (orders.length === 0) {
      return;
    }
    const stillAssigned = orders.some(
      (order) => order.order_id === selectedOrderId,
    );
    if (!stillAssigned) {
      setSelectedOrderId(orders[0].order_id);
    }
  }, [orders, selectedOrderId]);

  const thread = useQuery({
    queryKey: queryKeys.messages(selectedOrderId ?? ''),
    queryFn: () => loadThread(selectedOrderId ?? ''),
    enabled: Boolean(selectedOrderId),
  });

  const send = useMutation({
    mutationFn: (body: string) =>
      sendThreadMessage({ orderId: selectedOrderId ?? '', body }),
    onSuccess: async () => {
      setDraft('');
      setMessage(null);
      await queryClient.invalidateQueries({
        queryKey: queryKeys.messages(selectedOrderId ?? ''),
      });
    },
    onError: (error) => {
      setMessage(
        error instanceof Error
          ? error.message
          : 'The message could not be sent.',
      );
    },
  });

  const selectedOrder = orders.find(
    (order) => order.order_id === selectedOrderId,
  );
  const messages = thread.data?.data ?? [];

  return (
    <KeyboardAvoidingView
      className="flex-1 bg-background"
      behavior={Platform.OS === 'ios' ? 'padding' : undefined}
    >
      <ScrollView
        contentContainerClassName="gap-5 p-5 pb-28"
        keyboardShouldPersistTaps="handled"
        refreshControl={
          <RefreshControl
            refreshing={thread.isRefetching}
            onRefresh={() => void thread.refetch()}
          />
        }
      >
        <View className="gap-1">
          <Text className="text-3xl font-bold">Dispatch</Text>
          <Text className="text-muted-foreground">
            Messages stay inside the order you are working
          </Text>
        </View>

        {orders.length === 0 ? (
          <Card>
            <CardHeader>
              <CardTitle>No thread yet</CardTitle>
              <CardDescription>
                A dispatch thread opens with each assigned delivery.
              </CardDescription>
            </CardHeader>
          </Card>
        ) : (
          <View className="gap-2">
            <Text className="font-semibold">Order</Text>
            <View className="flex-row flex-wrap gap-2">
              {orders.map((order) => {
                const active = order.order_id === selectedOrderId;
                return (
                  <Pressable
                    key={order.order_id}
                    accessibilityRole="button"
                    accessibilityState={{ selected: active }}
                    onPress={() => setSelectedOrderId(order.order_id)}
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
                      {order.customer_name}
                    </Text>
                  </Pressable>
                );
              })}
            </View>
          </View>
        )}

        {selectedOrder && (
          <Card>
            <CardHeader>
              <CardTitle>{selectedOrder.customer_name}</CardTitle>
              <CardDescription>
                {selectedOrder.destination.address}
              </CardDescription>
            </CardHeader>
            <CardContent className="gap-3">
              {thread.isLoading && <Text>Loading the thread…</Text>}
              {thread.isError && (
                <Text className="text-sm text-destructive">
                  The thread could not be loaded. Pull down to try again.
                </Text>
              )}
              {!thread.isLoading && messages.length === 0 && (
                <Text className="text-muted-foreground">
                  No messages on this order yet.
                </Text>
              )}
              {messages.map((entry) => {
                const fromDriver = entry.sender_role === 'driver';
                return (
                  <View
                    key={entry.message_id}
                    className={
                      fromDriver
                        ? 'self-end rounded-2xl bg-primary/10 px-4 py-3'
                        : 'self-start rounded-2xl bg-muted px-4 py-3'
                    }
                  >
                    <Text className="text-xs uppercase text-muted-foreground">
                      {fromDriver ? 'You' : 'Dispatch'} ·{' '}
                      {timeOf(entry.timestamp)}
                    </Text>
                    <Text className="mt-1">{entry.body}</Text>
                  </View>
                );
              })}
            </CardContent>
          </Card>
        )}

        {message && <Text className="text-sm text-destructive">{message}</Text>}

        {selectedOrder && (
          <View className="gap-3">
            <Input
              value={draft}
              onChangeText={(next) => {
                setDraft(next);
                setMessage(null);
              }}
              placeholder="Message dispatch"
              multiline
              editable={!send.isPending}
            />
            <Button
              disabled={send.isPending || draft.trim().length === 0}
              onPress={() => send.mutate(draft.trim())}
            >
              <Text>{send.isPending ? 'Sending…' : 'Send'}</Text>
            </Button>
          </View>
        )}
      </ScrollView>
    </KeyboardAvoidingView>
  );
}
