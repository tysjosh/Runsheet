/**
 * The cross-contamination warning (R6.11) as an exception badge plus dark-red
 * text (UI revamp R13.7, design.md §8). The old `text-destructive` sentence was
 * #ef4444 on white, 3.76:1; red.800 is 8.3:1 on white, and the night theme uses
 * red.300 (9.4:1 on the night surface). Grades show as product names, not
 * codes; the sentence is otherwise `crossContaminationMessage`'s.
 */

import { View } from 'react-native';

import { StatusBadge } from '@/components/StatusBadge';
import { Text } from '@/components/ui/text';
import { useColorScheme } from '@/hooks/useColorScheme';
import { productName } from '@/lib/format';
import type { CompartmentLedgerRow } from '@/lib/route-api';
import { paletteFor } from '@/lib/theme';

export function contaminationText(row: CompartmentLedgerRow): string | null {
  if (!row.crossContaminationWarning) {
    return null;
  }
  const prior = row.priorGrade ? productName(row.priorGrade) : 'an unrecorded grade';
  return (
    `Compartment ${row.compartmentId} last held ${prior} and is now loaded ` +
    `with ${productName(row.loadedGrade)}. Confirm it was cleaned before you draw from it.`
  );
}

export function ContaminationWarning({ row }: { row: CompartmentLedgerRow }) {
  const palette = paletteFor(useColorScheme());
  const text = contaminationText(row);
  if (!text) {
    return null;
  }
  return (
    <View className="gap-2" testID={`contamination-${row.compartmentId}`}>
      <StatusBadge status="exception" label="Cross-contamination" />
      <Text className="font-semibold" style={{ color: palette.danger }}>
        {text}
      </Text>
    </View>
  );
}
