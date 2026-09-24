import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "../lib/cn";

const button = cva(
  "inline-flex items-center justify-center gap-1.5 rounded-md border font-medium transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:opacity-50 disabled:pointer-events-none select-none",
  {
    variants: {
      variant: {
        primary: "bg-accent text-accent-ink border-accent hover:opacity-90",
        secondary: "bg-surface text-ink border-line-2 hover:bg-surface-2",
        ghost: "bg-transparent text-ink-2 border-transparent hover:bg-surface-2",
        danger: "bg-bad-soft text-bad border-bad-soft hover:opacity-90",
      },
      size: {
        sm: "h-7 px-2.5 text-[12px]",
        md: "h-8 px-3 text-[13px]",
        lg: "h-10 px-4 text-[14px]",
      },
    },
    defaultVariants: { variant: "secondary", size: "md" },
  },
);

export type ButtonProps = React.ButtonHTMLAttributes<HTMLButtonElement> & VariantProps<typeof button>;

/** The one button. `primary` is the single most important action on a screen; `ghost` for toolbars. */
export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { className, variant, size, type = "button", ...props },
  ref,
) {
  return <button ref={ref} type={type} className={cn(button({ variant, size }), className)} {...props} />;
});
