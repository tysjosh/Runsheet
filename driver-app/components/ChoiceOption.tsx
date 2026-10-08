/**
 * ChoiceOption — one option of a single-choice group, sized for gloves (≥ 48 pt)
 * and announced as a radio with its checked state (UI revamp task 4.2: the old
 * `accessibilityState={{ selected }}` alone left `aria-checked` off on web,
 * which axe reports as `aria-required-attr`).
 *
 * `variant="card"` is the full-width option with an optional description;
 * `variant="chip"` is the compact pill used for long option lists.
 */

import type { ReactNode } from 'react';
import { Pressable, View } from 'react-native';

import { Text } from '@/components/ui/text';
import { cn } from '@/lib/utils';

export interface ChoiceOptionProps {
  label: string;
  description?: ReactNode;
  checked: boolean;
  disabled?: boolean;
  onPress: () => void;
  variant?: 'card' | 'chip';
  testID?: string;
}

export function ChoiceOption({
  label,
  description,
  checked,
  disabled = false,
  onPress,
  variant = 'card',
  testID,
}: ChoiceOptionProps) {
  const a11y = {
    accessibilityRole: 'radio' as const,
    accessibilityState: { checked, selected: checked, disabled },
    // react-native-web does not map `accessibilityState.checked` for radios;
    // `aria-checked` is what reaches the DOM (and native maps it too).
    'aria-checked': checked,
    disabled,
    onPress,
    testID,
  };

  if (variant === 'chip') {
    return (
      <Pressable
        {...a11y}
        className={cn(
          'min-h-12 justify-center rounded-full px-4',
          checked ? 'bg-primary' : 'border-2 border-input bg-background',
          disabled && 'opacity-50',
        )}
      >
        <Text className={cn('text-base', checked ? 'font-semibold text-primary-foreground' : 'text-foreground')}>
          {label}
        </Text>
      </Pressable>
    );
  }

  return (
    <Pressable
      {...a11y}
      className={cn(
        'min-h-12 flex-row items-center gap-3 rounded-xl p-3',
        checked ? 'border-2 border-primary bg-accent' : 'border-2 border-input bg-background',
        disabled && 'opacity-50',
      )}
    >
      <View
        aria-hidden
        className={cn(
          'h-6 w-6 items-center justify-center rounded-full border-2',
          checked ? 'border-primary' : 'border-muted-foreground',
        )}
      >
        {checked ? <View className="h-3 w-3 rounded-full bg-primary" /> : null}
      </View>
      <View className="flex-1 gap-0.5">
        <Text className="font-semibold">{label}</Text>
        {typeof description === 'string' ? (
          <Text className="text-sm text-muted-foreground">{description}</Text>
        ) : (
          description
        )}
      </View>
    </Pressable>
  );
}
