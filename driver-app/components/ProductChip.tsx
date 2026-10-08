/**
 * ProductCap / ProductChip — the API RP 1637 fill-cap colour and symbol with the
 * readable product name (UI revamp D5, R13.3). Colours come from the generated
 * `lib/tokens.ts`, so they match the staff app's `ProductChip` exactly. Raw
 * product codes never show as text; unknown codes get the neutral cap and a
 * humanised name.
 */

import { View } from 'react-native';

import { Text } from '@/components/ui/text';
import { useColorScheme } from '@/hooks/useColorScheme';
import { productName } from '@/lib/format';
import { DRIVER, PRODUCT, type ProductToken } from '@/lib/tokens';
import { cn } from '@/lib/utils';

/** Night-theme ring around every cap: night `text-muted`, ≥ 11:1 on the night surface. */
export const CAP_NIGHT_RING = DRIVER.night['text-muted'];

export function productToken(code: string | null | undefined): ProductToken {
  return (code && PRODUCT[code as keyof typeof PRODUCT]) || PRODUCT._unknown;
}

export function ProductCap({
  code,
  size = 28,
  decorative = false,
}: {
  code: string;
  size?: number;
  /** True when the name is shown next to the cap, so it isn't read twice. */
  decorative?: boolean;
}) {
  const token = productToken(code);
  const night = useColorScheme() === 'dark';
  const fontSize = token.symbol.length >= 3 ? size * 0.32 : token.symbol.length === 2 ? size * 0.38 : size * 0.46;
  return (
    <View
      // At night the dark caps (heating oil, kerosene, DEF) fall below 3:1 on
      // the night surface, so every cap gets a light ring (CAP_NIGHT_RING).
      // The cap colours themselves stay the RP 1637 ones (D5).
      style={
        night
          ? { padding: 2, borderRadius: size / 2 + 2, backgroundColor: CAP_NIGHT_RING }
          : undefined
      }
    >
      <View
        accessible={!decorative}
        accessibilityRole={decorative ? undefined : 'image'}
        accessibilityLabel={decorative ? undefined : productName(code)}
        importantForAccessibility={decorative ? 'no-hide-descendants' : 'auto'}
        aria-hidden={decorative || undefined}
        testID={`product-cap-${code}`}
        style={{
          width: size,
          height: size,
          borderRadius: size / 2,
          borderWidth: 2,
          backgroundColor: token.bg,
          borderColor: token.border,
          alignItems: 'center',
          justifyContent: 'center',
        }}
      >
        <Text style={{ color: token.fg, fontSize, fontWeight: '700', lineHeight: fontSize * 1.1 }}>
          {token.symbol}
        </Text>
      </View>
    </View>
  );
}

export function ProductChip({
  code,
  size = 28,
  className,
  suffix,
}: {
  code: string;
  size?: number;
  className?: string;
  /** Optional trailing text, e.g. the gallons for this grade. */
  suffix?: string;
}) {
  return (
    <View className={cn('flex-row items-center gap-2', className)}>
      <ProductCap code={code} size={size} decorative />
      <Text className="shrink font-semibold">{productName(code)}</Text>
      {suffix ? <Text className="text-muted-foreground">{suffix}</Text> : null}
    </View>
  );
}
