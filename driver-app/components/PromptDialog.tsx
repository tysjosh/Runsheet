/**
 * PromptDialog — NEW. The one prompt for driver text or numeric entry in this
 * app, rendering on both iOS and Android (R16.15).
 *
 * `Alert.prompt` — the donor's only entry surface, used for the delivery code —
 * is an iOS-only React Native API: on Android it silently renders a button-only
 * alert with no text field, so the driver has no way to answer. Nothing here is
 * copied or adapted from the donor (R16.21); it is a `Modal` over the app's own
 * `components/ui` primitives.
 *
 * The numeric mode is what the POD wizard uses when the server answers a
 * meter-ticket submission with 409 `POD_GALLONS_CONFIRMATION_REQUIRED` and the
 * driver has to type the gallon count (R5.11, R5.13). Volumes are labelled
 * through `unitLabel`, which callers set from `lib/units.ts` — this component
 * formats no unit of its own (R16.10).
 *
 * Requirements: 5.1, 16.15, 16.21
 */

import React from 'react';
import { KeyboardAvoidingView, Modal, Platform, View } from 'react-native';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Text } from '@/components/ui/text';

/** Text entry, or a number the caller receives already parsed. */
export type PromptMode = 'text' | 'number';

export interface PromptConstraints {
  mode?: PromptMode;
  /** Defaults to `true`: an empty answer is rejected rather than submitted. */
  required?: boolean;
  /** `number` mode only. Inclusive. */
  min?: number;
  /** `number` mode only. Inclusive. */
  max?: number;
  /** `text` mode only. */
  maxLength?: number;
}

export type PromptValidation =
  | { ok: true; value: string | number }
  | { ok: false; error: string };

/**
 * Validate and normalize what the driver typed.
 *
 * Exported so the rule is testable without mounting a modal, and so a caller
 * that pre-fills a value can check it before opening the dialog.
 */
export function validatePromptValue(
  raw: string,
  constraints: PromptConstraints = {},
): PromptValidation {
  const { mode = 'text', required = true, min, max, maxLength } = constraints;
  const trimmed = raw.trim();

  if (!trimmed) {
    return required ? { ok: false, error: 'Enter a value to continue.' } : { ok: true, value: '' };
  }

  if (mode === 'number') {
    const parsed = Number(trimmed);
    if (!Number.isFinite(parsed)) {
      return { ok: false, error: 'Enter a number.' };
    }
    if (typeof min === 'number' && parsed < min) {
      return { ok: false, error: `Enter ${min} or more.` };
    }
    if (typeof max === 'number' && parsed > max) {
      return { ok: false, error: `Enter ${max} or less.` };
    }
    return { ok: true, value: parsed };
  }

  if (typeof maxLength === 'number' && trimmed.length > maxLength) {
    return { ok: false, error: `Use ${maxLength} characters or fewer.` };
  }
  return { ok: true, value: trimmed };
}

export interface PromptDialogProps extends PromptConstraints {
  visible: boolean;
  title: string;
  /** Why the app is asking. Shown under the title. */
  message?: string;
  initialValue?: string;
  placeholder?: string;
  /** Unit shown beside the field, e.g. `gal`. Supplied by the caller (R16.10). */
  unitLabel?: string;
  submitLabel?: string;
  cancelLabel?: string;
  /** A `string` in `text` mode, a `number` in `number` mode. */
  onSubmit: (value: string | number) => void;
  onCancel: () => void;
}

export function PromptDialog({
  visible,
  title,
  message,
  initialValue = '',
  placeholder,
  unitLabel,
  submitLabel = 'Save',
  cancelLabel = 'Cancel',
  mode = 'text',
  required = true,
  min,
  max,
  maxLength,
  onSubmit,
  onCancel,
}: PromptDialogProps) {
  const [value, setValue] = React.useState(initialValue);
  const [error, setError] = React.useState<string | null>(null);

  // Every opening starts from the caller's value, so a dismissed prompt never
  // reopens holding the previous answer.
  React.useEffect(() => {
    if (visible) {
      setValue(initialValue);
      setError(null);
    }
  }, [visible, initialValue]);

  const handleSubmit = React.useCallback(() => {
    const result = validatePromptValue(value, { mode, required, min, max, maxLength });
    if (!result.ok) {
      setError(result.error);
      return;
    }
    setError(null);
    onSubmit(result.value);
  }, [value, mode, required, min, max, maxLength, onSubmit]);

  return (
    <Modal
      visible={visible}
      transparent
      animationType="fade"
      // Android hardware back closes the prompt rather than the screen behind it.
      onRequestClose={onCancel}
    >
      <KeyboardAvoidingView
        behavior={Platform.OS === 'ios' ? 'padding' : undefined}
        className="flex-1 items-center justify-center bg-black/50 px-6"
      >
        <View
          className="w-full max-w-md gap-4 rounded-2xl bg-background p-6"
          accessibilityViewIsModal
          accessibilityLabel={title}
        >
          <View className="gap-1">
            <Text className="text-lg font-semibold text-foreground">{title}</Text>
            {message ? (
              <Text className="text-sm text-muted-foreground">{message}</Text>
            ) : null}
          </View>

          <View className="flex-row items-center gap-2">
            <Input
              className="flex-1"
              value={value}
              onChangeText={(next) => {
                setValue(next);
                setError(null);
              }}
              placeholder={placeholder}
              keyboardType={mode === 'number' ? 'decimal-pad' : 'default'}
              maxLength={mode === 'text' ? maxLength : undefined}
              autoFocus
              editable={visible}
              onSubmitEditing={handleSubmit}
              returnKeyType="done"
              accessibilityLabel={title}
            />
            {unitLabel ? (
              <Text className="text-base text-muted-foreground">{unitLabel}</Text>
            ) : null}
          </View>

          {error ? (
            <Text className="text-sm text-destructive" accessibilityRole="alert">
              {error}
            </Text>
          ) : null}

          <View className="flex-row gap-3">
            <Button variant="outline" size="sm" className="flex-1" onPress={onCancel}>
              <Text>{cancelLabel}</Text>
            </Button>
            <Button size="sm" className="flex-1" onPress={handleSubmit}>
              <Text>{submitLabel}</Text>
            </Button>
          </View>
        </View>
      </KeyboardAvoidingView>
    </Modal>
  );
}
