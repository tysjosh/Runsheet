/**
 * StatusBadge — icon + label on the status tint from the generated tokens, so
 * colour is never the only signal (R5.4, R13.7). Same palette as the staff
 * app's `StatusBadge` (fg = 800 on bg = 100, ≥ 4.5:1 in both themes because the
 * badge carries its own light fill).
 */

import {
  BadgeCheck,
  CalendarClock,
  CalendarX,
  Check,
  CircleAlert,
  CircleCheck,
  CircleDashed,
  Clock,
  OctagonAlert,
  Send,
  TriangleAlert,
  Truck,
  X,
  type LucideIcon,
} from 'lucide-react-native';
import { View } from 'react-native';

import { Text } from '@/components/ui/text';
import { STATUS, type StatusKey } from '@/lib/tokens';
import { cn } from '@/lib/utils';

const ICONS: Record<string, LucideIcon> = {
  BadgeCheck,
  CalendarClock,
  CalendarX,
  Check,
  CircleAlert,
  CircleCheck,
  CircleDashed,
  Clock,
  OctagonAlert,
  Send,
  TriangleAlert,
  Truck,
  X,
};

/** Driver-app order statuses → status tokens. */
export function statusKeyForOrder(status: string | null | undefined): StatusKey {
  switch (status) {
    case 'dispatched':
      return 'dispatched';
    case 'in_transit':
      return 'in_transit';
    case 'delivered':
      return 'delivered';
    case 'failed':
    case 'on_hold':
      return 'exception';
    case 'cancelled':
      return 'cancelled';
    case 'scheduled':
    case 'confirmed':
      return 'planned';
    default:
      return 'draft';
  }
}

export function StatusBadge({
  status,
  label,
  className,
}: {
  status: StatusKey;
  /** Overrides the token label (e.g. "Cross-contamination"). */
  label?: string;
  className?: string;
}) {
  const token = STATUS[status];
  const Icon = ICONS[token.icon] ?? CircleDashed;
  const text = label ?? token.label;
  return (
    <View
      accessible
      accessibilityRole="text"
      accessibilityLabel={text}
      className={cn('flex-row items-center gap-1.5 self-start rounded-full border px-3 py-1', className)}
      style={{ backgroundColor: token.bg, borderColor: token.border, minHeight: 28 }}
    >
      <Icon size={16} color={token.fg} />
      <Text
        className="text-sm font-semibold"
        style={{
          color: token.fg,
          textDecorationLine: token.strike ? 'line-through' : 'none',
        }}
      >
        {text}
      </Text>
    </View>
  );
}
