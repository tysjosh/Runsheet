/**
 * Copied from azumi-rider/components/ui/input.tsx
 * Copied: 2026-07-29
 * Donor: azumi-rider (Expo SDK 53). One of the 13 `components/ui/` primitives
 * carried over as this app's primitive layer (Requirements 16.2, 16.3, 16.5).
 * Verbatim apart from the import aliases (`~/` → `@/`) and the UI revamp
 * sizing (48 pt height, 16 pt text, 2 px border). No domain logic.
 */

import * as React from 'react';
import { TextInput, type TextInputProps } from 'react-native';
import { cn } from '@/lib/utils';

function Input({
  className,
  placeholderClassName,
  ...props
}: TextInputProps & {
  ref?: React.RefObject<TextInput>;
}) {
  return (
    <TextInput
      className={cn(
        'web:flex web:w-full rounded-lg border-2 border-muted-foreground/70 bg-background px-3 text-base native:text-lg native:leading-[1.25] text-foreground placeholder:text-muted-foreground web:ring-offset-background file:border-0 file:bg-transparent file:font-medium web:focus-visible:outline-none web:focus-visible:ring-2 web:focus-visible:ring-ring web:focus-visible:ring-offset-2',
        // UI revamp task 4.2: 48 pt fields for gloved hands (R13.5); multi-line
        // fields start at two lines and grow. The border is muted-foreground at
        // 70 % (≈ 3.6:1 by day, ≈ 6.5:1 at night) so the field edge holds up
        // in sunlight (WCAG 1.4.11).
        props.multiline ? 'min-h-24 py-3' : 'h-12 web:py-2',
        props.editable === false && 'opacity-50 web:cursor-not-allowed',
        className
      )}
      placeholderClassName={cn('text-muted-foreground', placeholderClassName)}
      textAlignVertical={props.multiline ? 'top' : undefined}
      {...props}
    />
  );
}

export { Input };
