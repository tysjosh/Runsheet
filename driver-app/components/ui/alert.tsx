/**
 * Copied from azumi-rider/components/ui/alert.tsx
 * Copied: 2026-07-29
 * Donor: azumi-rider (Expo SDK 53). One of the 13 `components/ui/` primitives
 * carried over as this app's primitive layer (Requirements 16.2, 16.3, 16.5).
 * Verbatim apart from the import aliases (`~/` → `@/`). No domain logic.
 */

import { useTheme } from '@react-navigation/native';
import { cva, type VariantProps } from 'class-variance-authority';
import type { LucideIcon } from 'lucide-react-native';
import * as React from 'react';
import { View, type ViewProps } from 'react-native';
import { cn } from '@/lib/utils';
import { Text } from '@/components/ui/text';

const alertVariants = cva(
  'relative bg-background w-full rounded-lg border border-border p-4 shadow shadow-foreground/10',
  {
    variants: {
      variant: {
        default: '',
        destructive: 'border-destructive',
      },
    },
    defaultVariants: {
      variant: 'default',
    },
  }
);

function Alert({
  className,
  variant,
  children,
  icon: Icon,
  iconSize = 16,
  iconClassName,
  ...props
}: ViewProps &
  VariantProps<typeof alertVariants> & {
    ref?: React.RefObject<View>;
    icon: LucideIcon;
    iconSize?: number;
    iconClassName?: string;
  }) {
  const { colors } = useTheme();
  return (
    <View role="alert" className={alertVariants({ variant, className })} {...props}>
      <View className={cn('absolute left-3.5 top-4 -translate-y-0.5', iconClassName)}>
        <Icon
          size={iconSize}
          color={variant === 'destructive' ? colors.notification : colors.text}
        />
      </View>
      {children}
    </View>
  );
}

function AlertTitle({ className, ...props }: React.ComponentProps<typeof Text>) {
  return (
    <Text
      className={cn(
        'pl-7 mb-1 font-medium text-base leading-none tracking-tight text-foreground',
        className
      )}
      {...props}
    />
  );
}

function AlertDescription({ className, ...props }: React.ComponentProps<typeof Text>) {
  return (
    <Text className={cn('pl-7 text-sm leading-relaxed text-foreground', className)} {...props} />
  );
}

export { Alert, AlertDescription, AlertTitle };
