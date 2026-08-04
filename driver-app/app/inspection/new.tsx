/**
 * Vehicle inspection — one screen, both report types.
 *
 * Pre-trip and post-trip carry the identical field set and differ only by
 * `inspection_type` (R8.8), so there is one form here rather than two screens.
 * Pre-trip is the default because it is accepted in every tenant; post-trip
 * depends on the carrier having enabled the inspection workflow, and a tenant
 * that has not answers 400 `post_trip_intake_not_enabled`. The screen says so on
 * the control instead of leaving the driver to learn it from a rejection.
 *
 * The submission goes through the offline queue (R11.8) and so carries an
 * `X-Idempotency-Key` minted once at action time and reused on every retry
 * (R8.10, R11.6). That is what makes a walk-around filed in a yard with no signal
 * safe to drain twice.
 *
 * The odometer is entered and sent in **miles**, labelled `mi` by `lib/units.ts`
 * and converted nowhere (R8.3, R16.10).
 *
 * Defect photos are optional and are uploaded through the presign service the
 * moment they are taken, because a `file_ref` is what the report carries — not
 * bytes. One consequence is stated plainly to the driver rather than hidden: with
 * no service the photo cannot be attached, and the defect is still filed without
 * it. An `out_of_service` severity stops the truck and alerts dispatch in every
 * tenant (R8.5), which the control says before it is chosen.
 *
 * Requirements: 8.3, 8.4, 8.8, 8.10, 11.8, 15.8, 16.10
 */

import { CameraView, useCameraPermissions } from 'expo-camera';
import { Stack, useLocalSearchParams, useRouter } from 'expo-router';
import { useRef, useState } from 'react';
import { Pressable, ScrollView, View } from 'react-native';

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
  componentLabel,
  DEFECT_SEVERITY_OPTIONS,
  INSPECTION_COMPONENTS,
  INSPECTION_TYPE_OPTIONS,
  localCalendarDay,
  queueInspectionReport,
  type DefectSeverityValue,
  type InspectionComponent,
  type InspectionDefectDraft,
  type InspectionTypeValue,
} from '@/lib/inspection-api';
import { uploadPodArtifact } from '@/lib/pod-api';
import { DISTANCE_UNIT_LABEL } from '@/lib/units';

export default function NewInspectionScreen() {
  const router = useRouter();
  const params = useLocalSearchParams<{
    assetId?: string;
    inspectionType?: string;
  }>();
  const paramAssetId = Array.isArray(params.assetId)
    ? params.assetId[0]
    : params.assetId;
  const paramType = Array.isArray(params.inspectionType)
    ? params.inspectionType[0]
    : params.inspectionType;

  const [inspectionType, setInspectionType] = useState<InspectionTypeValue>(
    paramType === 'post_trip' ? 'post_trip' : 'pre_trip',
  );
  const [assetId, setAssetId] = useState(paramAssetId ?? '');
  const [odometer, setOdometer] = useState('');
  const [defects, setDefects] = useState<InspectionDefectDraft[]>([]);

  // The defect being drafted. `null` means the editor is closed.
  const [draftComponent, setDraftComponent] =
    useState<InspectionComponent | null>(null);
  const [draftSeverity, setDraftSeverity] =
    useState<DefectSeverityValue>('minor');
  const [draftNote, setDraftNote] = useState('');
  const [draftPhotoRefs, setDraftPhotoRefs] = useState<string[]>([]);

  const cameraRef = useRef<CameraView>(null);
  const [cameraPermission, requestCameraPermission] = useCameraPermissions();
  const [cameraOpen, setCameraOpen] = useState(false);
  const [uploading, setUploading] = useState(false);

  const [submitting, setSubmitting] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const resetDraft = () => {
    setDraftComponent(null);
    setDraftSeverity('minor');
    setDraftNote('');
    setDraftPhotoRefs([]);
  };

  const addDefect = () => {
    if (!draftComponent) {
      setMessage('Choose the component the defect is on.');
      return;
    }
    setDefects((current) => [
      ...current,
      {
        component: draftComponent,
        severity: draftSeverity,
        note: draftNote,
        photoRefs: draftPhotoRefs,
      },
    ]);
    resetDraft();
    setMessage(null);
  };

  const openCamera = async () => {
    setMessage(null);
    if (!cameraPermission?.granted) {
      const granted = await requestCameraPermission();
      if (!granted.granted) {
        setMessage('Camera permission is required to photograph a defect.');
        return;
      }
    }
    setCameraOpen(true);
  };

  const capture = async () => {
    const shot = await cameraRef.current?.takePictureAsync({
      base64: true,
      quality: 0.7,
      skipProcessing: false,
    });
    setCameraOpen(false);
    if (!shot?.base64) {
      setMessage('The photo could not be read. Take it again.');
      return;
    }
    setUploading(true);
    try {
      // The report carries `file_ref` values, so the bytes go up now. The ref
      // comes back tenant-prefixed and is validated again server-side (R15.8).
      const uploaded = await uploadPodArtifact({
        category: 'photo',
        base64: shot.base64,
        contentType: 'image/jpeg',
      });
      setDraftPhotoRefs((current) => [...current, uploaded.fileRef]);
    } catch (error) {
      setMessage(
        `${
          error instanceof Error ? error.message : 'The photo did not upload.'
        } The defect can still be filed without it.`,
      );
    } finally {
      setUploading(false);
    }
  };

  const submit = async () => {
    const trimmedAsset = assetId.trim();
    if (!trimmedAsset) {
      setMessage('Enter the unit number of the vehicle you inspected.');
      return;
    }
    const miles = Number(odometer);
    if (!Number.isFinite(miles) || miles < 0) {
      setMessage(`Enter the odometer reading in ${DISTANCE_UNIT_LABEL}.`);
      return;
    }

    setSubmitting(true);
    setMessage(null);
    try {
      const now = new Date();
      const result = await queueInspectionReport({
        report: {
          inspectionType,
          assetId: trimmedAsset,
          odometerMiles: miles,
          inspectionTimestamp: now.toISOString(),
          inspectionLocalDate: localCalendarDay(now),
          defects,
        },
      });
      setMessage(
        result.inserted
          ? 'Inspection queued. It sends as soon as there is service.'
          : 'That inspection is already queued.',
      );
      router.back();
    } catch (error) {
      setMessage(
        error instanceof Error
          ? error.message
          : 'The inspection could not be recorded.',
      );
    } finally {
      setSubmitting(false);
    }
  };

  const busy = submitting || uploading;

  if (cameraOpen) {
    return (
      <>
        <Stack.Screen options={{ title: 'Defect photo' }} />
        <View className="flex-1 gap-3 bg-background p-5">
          <Text className="text-xl font-bold">Photograph the defect</Text>
          <CameraView className="flex-1 rounded-xl" ref={cameraRef} facing="back" />
          <Button onPress={() => void capture()}>
            <Text>Take photo</Text>
          </Button>
          <Button variant="outline" onPress={() => setCameraOpen(false)}>
            <Text>Cancel</Text>
          </Button>
        </View>
      </>
    );
  }

  return (
    <>
      <Stack.Screen options={{ title: 'Vehicle inspection' }} />
      <ScrollView
        className="flex-1 bg-background"
        contentContainerClassName="gap-5 p-5 pb-12"
        keyboardShouldPersistTaps="handled"
      >
        <View className="gap-1">
          <Text className="text-2xl font-bold">Vehicle inspection</Text>
          <Text className="text-muted-foreground">
            Your walk-around, recorded against your driver id from this session.
          </Text>
        </View>

        <Card>
          <CardHeader>
            <CardTitle>Report type</CardTitle>
            <CardDescription>
              Both carry the same details. Only the type differs.
            </CardDescription>
          </CardHeader>
          <CardContent className="gap-2">
            {INSPECTION_TYPE_OPTIONS.map((option) => {
              const active = option.value === inspectionType;
              return (
                <Pressable
                  key={option.value}
                  accessibilityRole="radio"
                  accessibilityState={{ selected: active }}
                  disabled={busy}
                  onPress={() => setInspectionType(option.value)}
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
          </CardContent>
        </Card>

        <View className="gap-2">
          <Text className="font-medium">Vehicle</Text>
          <Input
            value={assetId}
            onChangeText={setAssetId}
            placeholder="Unit number"
            autoCapitalize="characters"
            editable={!busy}
          />
        </View>

        <View className="gap-2">
          <Text className="font-medium">Odometer ({DISTANCE_UNIT_LABEL})</Text>
          <Input
            value={odometer}
            onChangeText={setOdometer}
            placeholder="0"
            keyboardType="decimal-pad"
            editable={!busy}
          />
        </View>

        <Card>
          <CardHeader>
            <CardTitle>Defects</CardTitle>
            <CardDescription>
              {defects.length === 0
                ? 'None recorded. A clean walk-around is a valid report.'
                : `${defects.length} recorded on this report.`}
            </CardDescription>
          </CardHeader>
          <CardContent className="gap-3">
            {defects.map((defect, index) => (
              <View
                key={`${defect.component}-${index}`}
                className="gap-1 rounded-xl border border-input p-3"
              >
                <Text className="font-semibold">
                  {componentLabel(defect.component)}
                </Text>
                <Text
                  className={
                    defect.severity === 'out_of_service'
                      ? 'text-sm font-semibold text-destructive'
                      : 'text-sm text-muted-foreground'
                  }
                >
                  {defect.severity === 'out_of_service'
                    ? 'Out of service'
                    : 'Minor'}
                  {defect.photoRefs && defect.photoRefs.length > 0
                    ? ` · ${defect.photoRefs.length} photo(s)`
                    : ''}
                </Text>
                {defect.note.trim().length > 0 && (
                  <Text className="text-sm">{defect.note.trim()}</Text>
                )}
                <Button
                  size="sm"
                  variant="outline"
                  disabled={busy}
                  onPress={() =>
                    setDefects((current) =>
                      current.filter((_, position) => position !== index),
                    )
                  }
                >
                  <Text>Remove</Text>
                </Button>
              </View>
            ))}

            {draftComponent === null ? (
              <Button
                variant="outline"
                disabled={busy}
                onPress={() => setDraftComponent('service_brakes')}
              >
                <Text>Add a defect</Text>
              </Button>
            ) : (
              <View className="gap-3">
                <Text className="font-medium">Component</Text>
                <View className="flex-row flex-wrap gap-2">
                  {INSPECTION_COMPONENTS.map((component) => {
                    const active = component === draftComponent;
                    return (
                      <Pressable
                        key={component}
                        accessibilityRole="radio"
                        accessibilityState={{ selected: active }}
                        disabled={busy}
                        onPress={() => setDraftComponent(component)}
                        className={
                          active
                            ? 'rounded-full bg-primary px-3 py-2'
                            : 'rounded-full border border-input px-3 py-2'
                        }
                      >
                        <Text
                          className={
                            active
                              ? 'text-sm font-semibold text-primary-foreground'
                              : 'text-sm'
                          }
                        >
                          {componentLabel(component)}
                        </Text>
                      </Pressable>
                    );
                  })}
                </View>

                <Text className="font-medium">Severity</Text>
                {DEFECT_SEVERITY_OPTIONS.map((option) => {
                  const active = option.value === draftSeverity;
                  return (
                    <Pressable
                      key={option.value}
                      accessibilityRole="radio"
                      accessibilityState={{ selected: active }}
                      disabled={busy}
                      onPress={() => setDraftSeverity(option.value)}
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

                <Input
                  value={draftNote}
                  onChangeText={setDraftNote}
                  placeholder="What you found"
                  multiline
                  editable={!busy}
                />

                <Text className="text-sm text-muted-foreground">
                  {draftPhotoRefs.length === 0
                    ? 'No photo attached.'
                    : `${draftPhotoRefs.length} photo(s) attached.`}
                </Text>
                <Button
                  variant="outline"
                  disabled={busy}
                  onPress={() => void openCamera()}
                >
                  <Text>{uploading ? 'Uploading photo…' : 'Add a photo'}</Text>
                </Button>

                <Button disabled={busy} onPress={addDefect}>
                  <Text>Add defect</Text>
                </Button>
                <Button variant="outline" disabled={busy} onPress={resetDraft}>
                  <Text>Cancel defect</Text>
                </Button>
              </View>
            )}
          </CardContent>
        </Card>

        {message && (
          <View className="rounded-xl bg-muted p-4">
            <Text>{message}</Text>
          </View>
        )}

        <Button disabled={busy} onPress={() => void submit()}>
          <Text>
            {submitting
              ? 'Recording…'
              : inspectionType === 'post_trip'
                ? 'Submit post-trip inspection'
                : 'Submit pre-trip inspection'}
          </Text>
        </Button>
      </ScrollView>
    </>
  );
}
