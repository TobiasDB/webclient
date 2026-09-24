import * as React from "react";
import { cn } from "../lib/cn";

export type InputProps = React.InputHTMLAttributes<HTMLInputElement> & { mono?: boolean };

export const Input = React.forwardRef<HTMLInputElement, InputProps>(function Input({ className, mono, ...props }, ref) {
  return (
    <input
      ref={ref}
      className={cn(
        "h-8 w-full rounded-md border border-line-2 bg-surface px-2.5 text-[13px] text-ink placeholder:text-muted focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-accent",
        mono && "font-mono text-[12px]",
        className,
      )}
      {...props}
    />
  );
});

export type SelectProps = React.SelectHTMLAttributes<HTMLSelectElement>;

export const Select = React.forwardRef<HTMLSelectElement, SelectProps>(function Select({ className, ...props }, ref) {
  return (
    <select
      ref={ref}
      className={cn(
        "h-8 rounded-md border border-line-2 bg-surface px-2 text-[13px] text-ink focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-accent",
        className,
      )}
      {...props}
    />
  );
});
