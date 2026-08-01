/**
 * Copied from azumi-rider/components/ui/spinner.tsx
 * Copied: 2026-07-29
 * Donor: azumi-rider (Expo SDK 53). One of the 13 `components/ui/` primitives
 * carried over as this app's primitive layer (Requirements 16.2, 16.3, 16.5).
 * Verbatim apart from the `useEffect` dependency list, which this repository's
 * lint configuration requires, and the default arc colour, which no longer
 * carries the donor brand value. No domain logic.
 */

import { useEffect } from 'react';
import { View } from 'react-native';
import Animated, {
  Easing,
  useAnimatedStyle,
  useSharedValue,
  withRepeat,
  withTiming,
} from 'react-native-reanimated';

interface SpinnerProps {
  size?: number;
  color?: string;
  trackColor?: string;
  strokeWidth?: number;
}

export function Spinner({
  size = 48,
  color = '#2563eb',
  trackColor = '#e5e7eb',
  strokeWidth = 5,
}: SpinnerProps) {
  const rotation = useSharedValue(0);

  useEffect(() => {
    rotation.value = withRepeat(
      withTiming(360, {
        duration: 700,
        easing: Easing.linear,
      }),
      -1,
      false
    );
  }, [rotation]);

  const animatedStyle = useAnimatedStyle(() => {
    return {
      transform: [{ rotate: `${rotation.value}deg` }],
    };
  });

  const radius = size / 2;

  return (
    <View style={{ width: size, height: size }}>
      {/* Background track */}
      <View
        style={{
          position: 'absolute',
          width: size,
          height: size,
          borderRadius: radius,
          borderWidth: strokeWidth,
          borderColor: trackColor,
        }}
      />
      {/* Spinning arc */}
      <Animated.View
        style={[
          {
            position: 'absolute',
            width: size,
            height: size,
            borderRadius: radius,
            borderWidth: strokeWidth,
            borderTopColor: color,
            borderRightColor: color,
            borderBottomColor: 'transparent',
            borderLeftColor: 'transparent',
            borderCurve: 'continuous',
          },
          animatedStyle,
        ]}
      />
    </View>
  );
}
