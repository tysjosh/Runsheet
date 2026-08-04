/**
 * POD capture wizard: signature, delivery photos, meter ticket, gallons entry,
 * and the refusal path.
 *
 * Every artifact leaves this screen as a {@link CapturedArtifact} with its
 * category attached — `signature` for the finger-drawn path rendered to a raster
 * image (R5.1), `photo` for each delivery photograph (R5.2), `meter_ticket` for
 * the meter-ticket photograph (R5.3). `lib/pod-api.ts` owns the presign, the PUT,
 * the re-encode-and-retry on `max_file_bytes`, and the replacement URL on
 * `expires_at` (R5.4–R5.6); nothing about the upload contract lives here.
 *
 * A refusal needs neither signature, photo, nor gallon count (R5.14), so the
 * wizard's requirements change with the toggle rather than being enforced twice.
 *
 * Volume is US gallons throughout, labelled by `lib/units.ts`, and converted
 * nowhere (R5.19, R16.18, R16.19).
 *
 * Denied precise location does not stop a submission (R10.15): the geotag falls
 * back to the delivery address's own coordinates and the screen says so, because
 * `PODRequest.geotag` is required (R5.8) and inventing a GPS reading is not an
 * option.
 *
 * Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.14, 5.19, 10.15, 11.10, 16.19
 */

import { useQuery } from '@tanstack/react-query';
import { CameraView, useCameraPermissions } from 'expo-camera';
import { Stack, useLocalSearchParams, useRouter } from 'expo-router';
import React, { useMemo, useRef, useState } from 'react';
import { Pressable, ScrollView, View } from 'react-native';

import { PermissionBanner } from '@/components/PermissionBanner';
import { SignaturePad, type SignatureCapture } from '@/components/SignaturePad';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Text } from '@/components/ui/text';
import { requestGeotag } from '@/lib/geotag';
import {
  queuePodCapture,
  REFUSAL_REASONS,
  type CapturedArtifact,
  type RefusalReasonCode,
} from '@/lib/pod-api';
import { queryKeys } from '@/lib/query-keys';
import { formatGallons, VOLUME_UNIT_LABEL } from '@/lib/units';
import { loadWorkDetail } from '@/lib/work-api';

/** Which artifact the camera is currently capturing. */
type CameraTarget = 'photo' | 'meter_ticket';

export default function ProofOfDeliveryScreen() {
  const router = useRouter();
  const params = useLocalSearchParams<{
    orderId: string;
    customerId?: string;
  }>();
  const orderId = Array.isArray(params.orderId)
    ? params.orderId[0]
    : params.orderId;
  const customerId = Array.isArray(params.customerId)
    ? params.customerId[0]
    : params.customerId;

  const detail = useQuery({
    queryKey: queryKeys.order(orderId || ''),
    queryFn: () => loadWorkDetail(orderId || ''),
    enabled: Boolean(orderId),
  });

  const cameraRef = useRef<CameraView>(null);
  const [cameraPermission, requestCameraPermission] = useCameraPermissions();
  const [cameraTarget, setCameraTarget] = useState<CameraTarget | null>(null);

  const [recipientName, setRecipientName] = useState('');
  const [gallons, setGallons] = useState('');
  const [signature, setSignature] = useState<SignatureCapture | null>(null);
  const [photos, setPhotos] = useState<CapturedArtifact[]>([]);
  const [meterTicket, setMeterTicket] = useState<CapturedArtifact | null>(null);

  const [refused, setRefused] = useState(false);
  const [refusalReason, setRefusalReason] = useState<RefusalReasonCode | null>(
    null,
  );
  const [refusalNote, setRefusalNote] = useState('');

  const [submitting, setSubmitting] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [locationDenied, setLocationDenied] = useState(false);

  const order = detail.data;
  const numericGallons = Number(gallons);

  const canSubmit = useMemo(() => {
    if (!orderId || !recipientName.trim()) {
      return false;
    }
    if (refused) {
      // R5.14 — a refusal needs a reason code and nothing else.
      return Boolean(refusalReason);
    }
    return Boolean(
      signature &&
        photos.length > 0 &&
        Number.isFinite(numericGallons) &&
        numericGallons > 0,
    );
  }, [
    numericGallons,
    orderId,
    photos.length,
    recipientName,
    refusalReason,
    refused,
    signature,
  ]);

  const openCamera = async (target: CameraTarget) => {
    setMessage(null);
    if (!cameraPermission?.granted) {
      const result = await requestCameraPermission();
      if (!result.granted) {
        setMessage('Camera permission is required for delivery evidence.');
        return;
      }
    }
    setCameraTarget(target);
  };

  const capture = async () => {
    const target = cameraTarget;
    if (!target) {
      return;
    }
    const result = await cameraRef.current?.takePictureAsync({
      base64: true,
      quality: 0.7,
      skipProcessing: false,
    });
    if (!result?.base64) {
      setMessage('The photo could not be read. Please take it again.');
      return;
    }
    const artifact: CapturedArtifact = {
      category: target,
      base64: result.base64,
      contentType: 'image/jpeg',
    };
    if (target === 'photo') {
      setPhotos((current) => [...current, artifact]);
    } else {
      setMeterTicket(artifact);
    }
    setCameraTarget(null);
  };

  const submit = async () => {
    if (!canSubmit || !orderId) {
      setMessage(
        refused
          ? 'Record the recipient and a refusal reason.'
          : 'Complete the recipient, gallons, photo and signature.',
      );
      return;
    }
    setSubmitting(true);
    setMessage('Saving POD evidence…');
    try {
      const { fix, permission } = await requestGeotag();
      setLocationDenied(permission === 'denied');

      // R5.8 requires a geotag; R10.15 requires the submission to proceed with
      // location sharing off. The delivery address's own coordinates are the one
      // honest substitute, and the driver is told which one was used.
      const fallback = order?.destination;
      const geotag = fix
        ? { lat: fix.lat, lng: fix.lng }
        : fallback && Number.isFinite(fallback.lat) && Number.isFinite(fallback.lon)
          ? { lat: fallback.lat, lng: fallback.lon }
          : null;
      if (!geotag) {
        throw new Error(
          'This POD needs a location and none is available. Turn location on ' +
            'for this app and try again.',
        );
      }

      const artifacts: CapturedArtifact[] = [
        ...(signature
          ? [
              {
                category: 'signature' as const,
                base64: signature.base64,
                contentType: signature.mimeType,
              },
            ]
          : []),
        ...photos,
        ...(meterTicket ? [meterTicket] : []),
      ];

      const result = await queuePodCapture({
        orderId,
        artifacts,
        pod: {
          recipient_name: recipientName.trim(),
          ...(customerId ? { customer_id: customerId } : {}),
          ...(refused
            ? {
                refused_delivery: true,
                ...(refusalReason ? { refusal_reason_code: refusalReason } : {}),
                ...(refusalNote.trim()
                  ? { refusal_note: refusalNote.trim() }
                  : {}),
              }
            : { delivered_gallons: numericGallons }),
          geotag,
          timestamp: new Date().toISOString(),
        },
      });

      const locationNote =
        fix?.source === 'precise'
          ? ''
          : fix
            ? ' Location came from the last known position.'
            : ' Location sharing is off, so the delivery address was used.';
      setMessage(
        (result.synced
          ? refused
            ? 'Refusal submitted.'
            : 'POD submitted.'
          : 'POD saved on this device and will sync when service returns.') +
          locationNote,
      );
      router.back();
    } catch (error) {
      setMessage(
        error instanceof Error ? error.message : 'POD submission failed.',
      );
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <>
      <Stack.Screen
        options={{
          title: refused ? 'Refused delivery' : 'Proof of delivery',
          headerBackTitle: 'Order',
        }}
      />
      <ScrollView
        className="flex-1 bg-background"
        contentContainerClassName="gap-6 p-5 pb-12"
        keyboardShouldPersistTaps="handled"
      >
        <View className="gap-1">
          <Text className="text-2xl font-bold">
            {refused ? 'Record a refusal' : 'Proof of delivery'}
          </Text>
          <Text className="text-muted-foreground">
            {refused
              ? 'A refusal needs a reason code. Signature, photos and gallons are optional.'
              : `Record the ${VOLUME_UNIT_LABEL} that actually entered the customer tank.`}
          </Text>
          {order && (
            <Text className="text-sm text-muted-foreground">
              {order.customer_name} · {order.product_grade} ·{' '}
              {formatGallons(order.ordered_gallons)} ordered
            </Text>
          )}
        </View>

        {locationDenied && <PermissionBanner permissions={{ location: 'denied' }} />}

        <View className="gap-2">
          <Text className="font-medium">Recipient name</Text>
          <Input
            value={recipientName}
            onChangeText={setRecipientName}
            placeholder="Customer or site representative"
            editable={!submitting}
          />
        </View>

        <Button
          variant={refused ? 'default' : 'outline'}
          disabled={submitting}
          onPress={() => {
            setRefused((current) => !current);
            setMessage(null);
          }}
        >
          <Text>
            {refused ? 'Back to a completed delivery' : 'The delivery was refused'}
          </Text>
        </Button>

        {refused ? (
          <Card>
            <CardHeader>
              <CardTitle>Refusal reason</CardTitle>
              <CardDescription>
                One of the eight reason codes the office reconciles against.
              </CardDescription>
            </CardHeader>
            <CardContent className="gap-2">
              {REFUSAL_REASONS.map((option) => {
                const active = option.value === refusalReason;
                return (
                  <Pressable
                    key={option.value}
                    accessibilityRole="radio"
                    accessibilityState={{ selected: active }}
                    disabled={submitting}
                    onPress={() => setRefusalReason(option.value)}
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
              <Input
                value={refusalNote}
                onChangeText={setRefusalNote}
                placeholder="What happened (optional)"
                multiline
                editable={!submitting}
              />
            </CardContent>
          </Card>
        ) : (
          <View className="gap-2">
            <Text className="font-medium">
              Actual gallons delivered ({VOLUME_UNIT_LABEL})
            </Text>
            <Input
              value={gallons}
              onChangeText={setGallons}
              placeholder="0.0"
              keyboardType="decimal-pad"
              editable={!submitting}
            />
          </View>
        )}

        {cameraTarget ? (
          <View className="gap-3">
            <Text className="font-medium">
              {cameraTarget === 'photo' ? 'Delivery photo' : 'Meter ticket'}
            </Text>
            <CameraView
              ref={cameraRef}
              facing="back"
              style={{ height: 360, borderRadius: 16, overflow: 'hidden' }}
            />
            <Button onPress={() => void capture()}>
              <Text>Take photo</Text>
            </Button>
            <Button variant="outline" onPress={() => setCameraTarget(null)}>
              <Text>Cancel</Text>
            </Button>
          </View>
        ) : (
          <View className="gap-4">
            <View className="gap-2">
              <Text className="font-medium">
                Delivery photos ({photos.length})
              </Text>
              <Button
                variant={photos.length > 0 ? 'outline' : 'default'}
                disabled={submitting}
                onPress={() => void openCamera('photo')}
              >
                <Text>
                  {photos.length > 0 ? 'Add another photo' : 'Capture delivery photo'}
                </Text>
              </Button>
              {photos.length > 0 && (
                <Button
                  variant="outline"
                  size="sm"
                  disabled={submitting}
                  onPress={() => setPhotos((current) => current.slice(0, -1))}
                >
                  <Text>Remove the last photo</Text>
                </Button>
              )}
            </View>

            <View className="gap-2">
              <Text className="font-medium">Meter ticket</Text>
              <Button
                variant={meterTicket ? 'outline' : 'default'}
                disabled={submitting}
                onPress={() => void openCamera('meter_ticket')}
              >
                <Text>
                  {meterTicket ? 'Retake meter ticket' : 'Capture meter ticket'}
                </Text>
              </Button>
              {meterTicket && (
                <Text className="text-sm text-green-700">
                  Meter ticket captured
                </Text>
              )}
            </View>
          </View>
        )}

        {!refused && (
          <>
            <SignaturePad
              onCapture={(next) => {
                setSignature(next);
                setMessage(null);
              }}
              onError={setMessage}
              disabled={submitting}
              confirmLabel={signature ? 'Replace signature' : 'Use signature'}
            />
            {signature && (
              <Text className="text-sm text-green-700">Signature captured</Text>
            )}
          </>
        )}

        {message && (
          <View className="rounded-xl bg-muted p-4">
            <Text>{message}</Text>
          </View>
        )}

        <Button onPress={() => void submit()} disabled={!canSubmit || submitting}>
          <Text>
            {submitting
              ? 'Saving…'
              : refused
                ? 'Submit refusal'
                : 'Complete delivery'}
          </Text>
        </Button>
      </ScrollView>
    </>
  );
}
