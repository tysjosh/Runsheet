/**
 * Copied from azumi-rider/components/ui/skeleton.tsx
 * Copied: 2026-07-29
 * Donor: azumi-rider (Expo SDK 53). One of the 13 `components/ui/` primitives
 * carried over as this app's primitive layer (Requirements 16.2, 16.3, 16.5).
 * Verbatim apart from the import aliases (`~/` → `@/`) and the `useEffect`
 * dependency list, which this repository's lint configuration requires. No
 * domain logic.
 */

import * as React from 'react';
import Animated, {
  useAnimatedStyle,
  useSharedValue,
  withRepeat,
  withSequence,
  withTiming,
} from 'react-native-reanimated';
import { cn } from '@/lib/utils';

const duration = 1000;

function Skeleton({
  className,
  ...props
}: Omit<React.ComponentPropsWithoutRef<typeof Animated.View>, 'style'>) {
  const sv = useSharedValue(1);

  React.useEffect(() => {
    sv.value = withRepeat(
      withSequence(withTiming(0.5, { duration }), withTiming(1, { duration })),
      -1
    );
  }, [sv]);

  const style = useAnimatedStyle(() => ({
    opacity: sv.value,
  }));

  return (
    <Animated.View
      style={style}
      className={cn('rounded-md bg-secondary dark:bg-muted', className)}
      {...props}
    />
  );
}

export { Skeleton };
