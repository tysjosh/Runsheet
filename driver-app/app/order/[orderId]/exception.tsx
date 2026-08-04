/**
 * Exception report — one of the seven types, a severity, a note, and a geotag.
 *
 * The report goes through the offline queue (R11.8): a breakdown is exactly the
 * moment the radio is least likely to work. `high` and `critical` escalate to
 * dispatch server-side (R7.3), which is stated on the control so the driver knows
 * what the choice does.
 *
 * A denied precise location does not stop the report — the geotag is simply
 * omitted, the same way R10.15 keeps status transitions and POD working.
 *
 * Requirements: 7.1, 7.3, 7.13, 10.15, 11.8, 11.10
 */

import { useQueryClient } from '@tanstack/react-query';
import { Stack, useLocalSearchParams, useRouter } from 'expo-router';
import { useState } from 'react';
import { Pressable, ScrollView, View } from 'react-native';

import { PermissionBanner } from '@/components/PermissionBanner';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Text } from '@/components/ui/text';
import {
  EXCEPTION_SEVERITIES,
  EXCEPTION_TYPES,
  queueOrderException,
  type ExceptionTypeValue,
  type SeverityValue,
} from '@/lib/exception-api';
import { requestGeotag } from '@/lib/geotag';
import { WORK_SCOPE } from '@/lib/query-keys';

export default function ExceptionReportScreen() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const params = useLocalSearchParams<{ orderId: string }>();
  const orderId = Array.isArray(params.orderId)
    ? params.orderId[0]
    : params.orderId;

  const [exceptionType, setExceptionType] =
    useState<ExceptionTypeValue>('vehicle_breakdown');
  const [severity, setSeverity] = useState<SeverityValue>('medium');
  const [note, setNote] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [locationDenied, setLocationDenied] = useState(false);

  const submit = async () => {
    if (!orderId) {
      setMessage('This report has no order to attach to.');
      return;
    }
    if (!note.trim()) {
      setMessage('Describe what happened so dispatch can act on it.');
      return;
    }
    setSubmitting(true);
    setMessage(null);
    try {
      const { fix, permission } = await requestGeotag();
      setLocationDenied(permission === 'denied');
      const result = await queueOrderException({
        orderId,
        report: {
          exceptionType,
          severity,
          note,
          geotag: fix ? { lat: fix.lat, lng: fix.lng } : null,
        },
      });
      await queryClient.invalidateQueries({ queryKey: [WORK_SCOPE] });
      setMessage(
        result.inserted
          ? 'Exception queued. It sends as soon as there is service.'
          : 'That exception is already queued.',
      );
      router.back();
    } catch (error) {
      setMessage(
        error instanceof Error
          ? error.message
          : 'The exception could not be recorded.',
      );
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <>
      <Stack.Screen options={{ title: 'Report a problem' }} />
      <ScrollView
        className="flex-1 bg-background"
        contentContainerClassName="gap-5 p-5 pb-12"
        keyboardShouldPersistTaps="handled"
      >
        <View className="gap-1">
          <Text className="text-2xl font-bold">Report a problem</Text>
          <Text className="text-muted-foreground">
            Dispatch sees this against the order you are working.
          </Text>
        </View>

        {locationDenied && <PermissionBanner permissions={{ location: 'denied' }} />}

        <Card>
          <CardHeader>
            <CardTitle>What happened</CardTitle>
          </CardHeader>
          <CardContent className="gap-2">
            {EXCEPTION_TYPES.map((option) => {
              const active = option.value === exceptionType;
              return (
                <Pressable
                  key={option.value}
                  accessibilityRole="radio"
                  accessibilityState={{ selected: active }}
                  disabled={submitting}
                  onPress={() => setExceptionType(option.value)}
                  className={
                    active
                      ? 'rounded-xl border-2 border-primary p-3'
                      : 'rounded-xl border border-input p-3'
                  }
                >
                  <Text>{option.label}</Text>
                </Pressable>
              );
            })}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>How urgent</CardTitle>
            <CardDescription>
              High and critical reach dispatch immediately.
            </CardDescription>
          </CardHeader>
          <CardContent className="gap-2">
            {EXCEPTION_SEVERITIES.map((option) => {
              const active = option.value === severity;
              return (
                <Pressable
                  key={option.value}
                  accessibilityRole="radio"
                  accessibilityState={{ selected: active }}
                  disabled={submitting}
                  onPress={() => setSeverity(option.value)}
                  className={
                    active
                      ? 'rounded-xl border-2 border-primary p-3'
                      : 'rounded-xl border border-input p-3'
                  }
                >
                  <Text className="font-semibold">{option.label}</Text>
                  <Text className="text-sm text-muted-foreground">
                    {option.effect}
                  </Text>
                </Pressable>
              );
            })}
          </CardContent>
        </Card>

        <View className="gap-2">
          <Text className="font-medium">Details</Text>
          <Input
            value={note}
            onChangeText={setNote}
            placeholder="What dispatch needs to know"
            multiline
            editable={!submitting}
          />
        </View>

        {message && (
          <View className="rounded-xl bg-muted p-4">
            <Text>{message}</Text>
          </View>
        )}

        <Button disabled={submitting} onPress={() => void submit()}>
          <Text>{submitting ? 'Recording…' : 'Send to dispatch'}</Text>
        </Button>
      </ScrollView>
    </>
  );
}
