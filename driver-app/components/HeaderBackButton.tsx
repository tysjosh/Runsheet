/**
 * A 44×44 back button for the web build's stack header (UI revamp task 4.2).
 * Native platforms keep their system back button, which is already ≥ 44 pt.
 */

import { useRouter } from 'expo-router';
import { ChevronLeft } from 'lucide-react-native';
import { Pressable } from 'react-native';

import { useColorScheme } from '@/hooks/useColorScheme';
import { paletteFor } from '@/lib/theme';

export function HeaderBackButton() {
  const router = useRouter();
  const palette = paletteFor(useColorScheme());
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel="Back"
      onPress={() => router.back()}
      hitSlop={4}
      style={{
        minWidth: 44,
        minHeight: 44,
        alignItems: 'center',
        justifyContent: 'center',
        marginLeft: 4,
      }}
    >
      <ChevronLeft size={28} color={palette.tint} />
    </Pressable>
  );
}
