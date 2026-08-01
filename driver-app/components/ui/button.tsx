/**
 * Copied from azumi-rider/components/ui/button.tsx
 * Copied: 2026-07-29
 * Donor: azumi-rider (Expo SDK 53). One of the 13 `components/ui/` primitives
 * carried over as this app's primitive layer (Requirements 16.2, 16.3, 16.5).
 * Verbatim apart from the import aliases (`~/` → `@/`). No domain logic.
 */

import { cva, type VariantProps } from 'class-variance-authority';
import * as React from 'react';
import { Pressable } from 'react-native';
import { TextClassContext } from '@/components/ui/text';
import { cn } from '@/lib/utils';

const buttonVariants = cva(
  'group flex items-center justify-center rounded-xl web:ring-offset-background web:transition-colors web:focus-visible:outline-none web:focus-visible:ring-2 web:focus-visible:ring-ring web:focus-visible:ring-offset-2',
  {
    variants: {
      variant: {
        default: 'bg-primary web:hover:opacity-90 active:opacity-85',
        destructive: 'bg-destructive web:hover:opacity-90 active:opacity-85',
        outline:
          'border-2 border-input bg-background web:hover:bg-accent web:hover:text-accent-foreground active:bg-accent/50',
        secondary: 'bg-secondary web:hover:opacity-80 active:opacity-85',
        ghost: 'web:hover:bg-accent web:hover:text-accent-foreground active:bg-accent/50',
        link: 'web:underline-offset-4 web:hover:underline web:focus:underline',
      },
      size: {
        default: 'h-14 px-6 py-3.5 native:h-14 native:px-6 native:py-3.5',
        sm: 'h-11 rounded-xl px-4',
        lg: 'h-16 rounded-xl px-8 native:h-16',
        icon: 'h-12 w-12 rounded-xl',
      },
    },
    defaultVariants: {
      variant: 'default',
      size: 'lg',
    },
  }
);

const buttonTextVariants = cva(
  'web:whitespace-nowrap text-base native:text-base font-semibold text-foreground web:transition-colors',
  {
    variants: {
      variant: {
        default: 'text-primary-foreground ',
        destructive: 'text-destructive-foreground',
        outline: 'group-active:text-accent-foreground',
        secondary: 'text-secondary-foreground group-active:text-secondary-foreground',
        ghost: 'group-active:text-accent-foreground',
        link: 'text-primary group-active:underline',
      },
      size: {
        default: '',
        sm: 'text-sm native:text-sm',
        lg: 'native:text-lg text-lg',
        icon: '',
      },
    },
    defaultVariants: {
      variant: 'default',
      size: 'default',
    },
  }
);

type ButtonProps = React.ComponentProps<typeof Pressable> & VariantProps<typeof buttonVariants>;

function Button({ ref, className, variant, size, ...props }: ButtonProps) {
  return (
    <TextClassContext.Provider
      value={buttonTextVariants({
        variant,
        size,
        className: 'web:pointer-events-none',
      })}
    >
      <Pressable
        className={cn(
          props.disabled && 'opacity-50 web:pointer-events-none',
          buttonVariants({ variant, size, className })
        )}
        ref={ref}
        role="button"
        {...props}
      />
    </TextClassContext.Provider>
  );
}

export { Button, buttonTextVariants, buttonVariants };
export type { ButtonProps };
