/**
 * SignaturePad — NEW. Nothing here is copied, derived, or adapted from any
 * `/azumi-rider` path (R16.21): the donor carries no signature-capture
 * dependency at all, and its only delivery proof is a numeric code prompted
 * through the iOS-only `Alert.prompt`.
 *
 * Captures the customer signature as a finger-drawn path and hands the caller
 * the rendered raster image, base64-encoded, ready for the presigned PUT with
 * category `signature` (R5.1). The vector→raster step happens inside
 * `react-native-signature-canvas`, which returns a PNG data URL from
 * `readSignature()`; {@link parseSignatureDataUrl} is the only place that data
 * URL is unwrapped.
 *
 * Base64 is the encoding `lib/artifact-store.ts` retains and the encoding the
 * presigned PUT consumes, so the bytes are never re-coded on the way through.
 * No signature byte is ever passed to a log statement.
 *
 * Requirements: 5.1, 16.15 (the pad replaces the donor's `Alert.prompt` proof
 * path), 16.16, 16.21
 */

import React from 'react';
import { View } from 'react-native';
import { Check, Eraser, Undo2 } from 'lucide-react-native';
import SignatureView, {
  type SignatureViewRef,
} from 'react-native-signature-canvas';
import { Button } from '@/components/ui/button';
import { Text } from '@/components/ui/text';
import { cn } from '@/lib/utils';

/** The POD upload category every signature artifact carries (R5.1). */
export const SIGNATURE_ARTIFACT_CATEGORY = 'signature';

/** The raster format the pad renders to. One of the presign service's MIME types. */
export const SIGNATURE_MIME_TYPE = 'image/png';

/** A captured signature, ready for the presign request and the PUT (R5.4). */
export interface SignatureCapture {
  /** The raster bytes, base64-encoded, with no data-URL prefix. */
  base64: string;
  /** Always `image/png` — the pad renders to a single format. */
  mimeType: typeof SIGNATURE_MIME_TYPE;
  /** Always `signature` — the presign category for this artifact (R5.1). */
  category: typeof SIGNATURE_ARTIFACT_CATEGORY;
}

const PNG_DATA_URL = /^data:image\/png;base64,(.+)$/i;

/**
 * Unwrap the data URL the canvas returns into the base64 bytes the artifact
 * store and the presigned PUT both want.
 *
 * The webview payload can arrive with embedded newlines, so whitespace is
 * stripped first. A bare base64 string is accepted — some webview versions omit
 * the prefix — but a data URL declaring any format other than PNG is rejected
 * rather than mislabelled, because the `content_type` on the presign request has
 * to match the bytes actually uploaded (R5.4).
 *
 * @returns the capture, or `null` when there is nothing usable to upload.
 */
export function parseSignatureDataUrl(
  value: string | null | undefined,
): SignatureCapture | null {
  if (!value) {
    return null;
  }
  const compact = value.replace(/\s/g, '');
  if (!compact) {
    return null;
  }
  const match = PNG_DATA_URL.exec(compact);
  const base64 = match ? match[1] : compact.startsWith('data:') ? null : compact;
  if (!base64) {
    return null;
  }
  return {
    base64,
    mimeType: SIGNATURE_MIME_TYPE,
    category: SIGNATURE_ARTIFACT_CATEGORY,
  };
}

export interface SignaturePadProps {
  /** Called once the drawn path has been rendered and read back. */
  onCapture: (capture: SignatureCapture) => void;
  /** Surfaced when the pad is empty or the read produced nothing usable. */
  onError?: (message: string) => void;
  /** Fires on every transition between "nothing drawn" and "something drawn". */
  onStrokesChange?: (hasStrokes: boolean) => void;
  /** Instruction line above the pad. */
  label?: string;
  /** Label on the confirm control. */
  confirmLabel?: string;
  /** Pad height in points. Tall enough for a finger signature by default. */
  height?: number;
  penColor?: string;
  /** Blocks drawing and every control, e.g. while an upload is in flight. */
  disabled?: boolean;
  className?: string;
}

/**
 * The canvas ships its own footer with Clear and Confirm buttons; it is hidden
 * so the controls come from the app's own `components/ui/button` primitive
 * instead, and the pad fills its container edge to edge.
 */
const CANVAS_WEB_STYLE = `
  .m-signature-pad { box-shadow: none; border: none; margin: 0; }
  .m-signature-pad--body { border: none; }
  .m-signature-pad--footer { display: none; margin: 0; }
  body, html { width: 100%; height: 100%; margin: 0; background-color: transparent; }
`;

const EMPTY_MESSAGE = 'Ask the customer to sign before continuing.';
const UNREADABLE_MESSAGE = 'The signature could not be read. Clear it and try again.';

export function SignaturePad({
  onCapture,
  onError,
  onStrokesChange,
  label = 'Customer signature',
  confirmLabel = 'Use signature',
  height = 260,
  penColor = '#111827',
  disabled = false,
  className,
}: SignaturePadProps) {
  const padRef = React.useRef<SignatureViewRef>(null);
  const [hasStrokes, setHasStrokes] = React.useState(false);

  const setStrokes = React.useCallback(
    (next: boolean) => {
      setHasStrokes((current) => {
        if (current !== next) {
          onStrokesChange?.(next);
        }
        return next;
      });
    },
    [onStrokesChange],
  );

  const handleBegin = React.useCallback(() => setStrokes(true), [setStrokes]);

  const handleClear = React.useCallback(() => {
    padRef.current?.clearSignature();
    setStrokes(false);
  }, [setStrokes]);

  const handleUndo = React.useCallback(() => {
    padRef.current?.undo();
  }, []);

  const handleConfirm = React.useCallback(() => {
    if (!hasStrokes) {
      onError?.(EMPTY_MESSAGE);
      return;
    }
    padRef.current?.readSignature();
  }, [hasStrokes, onError]);

  const handleOk = React.useCallback(
    (dataUrl: string) => {
      const capture = parseSignatureDataUrl(dataUrl);
      if (!capture) {
        onError?.(UNREADABLE_MESSAGE);
        return;
      }
      onCapture(capture);
    },
    [onCapture, onError],
  );

  const handleEmpty = React.useCallback(() => {
    setStrokes(false);
    onError?.(EMPTY_MESSAGE);
  }, [onError, setStrokes]);

  return (
    <View className={cn('gap-3', className)}>
      <Text className="text-sm text-muted-foreground">{label}</Text>
      <View
        className="overflow-hidden rounded-xl border-2 border-input bg-background"
        style={{ height }}
        pointerEvents={disabled ? 'none' : 'auto'}
      >
        <SignatureView
          ref={padRef}
          onOK={handleOk}
          onEmpty={handleEmpty}
          onBegin={handleBegin}
          onClear={() => setStrokes(false)}
          penColor={penColor}
          imageType={SIGNATURE_MIME_TYPE}
          backgroundColor="rgba(255,255,255,1)"
          trimWhitespace
          webStyle={CANVAS_WEB_STYLE}
          style={{ flex: 1 }}
        />
      </View>
      <View className="flex-row gap-3">
        <Button
          variant="outline"
          size="sm"
          className="flex-1 flex-row gap-2"
          onPress={handleUndo}
          disabled={disabled || !hasStrokes}
          accessibilityLabel="Undo the last signature stroke"
        >
          <Undo2 size={16} color={penColor} />
          <Text>Undo</Text>
        </Button>
        <Button
          variant="outline"
          size="sm"
          className="flex-1 flex-row gap-2"
          onPress={handleClear}
          disabled={disabled || !hasStrokes}
          accessibilityLabel="Clear the signature"
        >
          <Eraser size={16} color={penColor} />
          <Text>Clear</Text>
        </Button>
        <Button
          size="sm"
          className="flex-1 flex-row gap-2"
          onPress={handleConfirm}
          disabled={disabled || !hasStrokes}
          accessibilityLabel={confirmLabel}
        >
          <Check size={16} color="#ffffff" />
          <Text>{confirmLabel}</Text>
        </Button>
      </View>
    </View>
  );
}
