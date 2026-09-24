import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "../lib/cn";

const chip = cva(
  "inline-flex items-center gap-1 rounded-full border px-2 h-6 text-[12px] leading-none whitespace-nowrap",
  {
    variants: {
      tone: {
        neutral: "bg-surface-2 text-ink-2 border-line",
        accent: "bg-accent-soft text-accent border-accent-soft",
        ok: "bg-ok-soft text-ok border-ok-soft",
        warn: "bg-warn-soft text-warn border-warn-soft",
        bad: "bg-bad-soft text-bad border-bad-soft",
      },
      interactive: { true: "cursor-pointer hover:brightness-95", false: "" },
    },
    defaultVariants: { tone: "neutral", interactive: false },
  },
);

export type ChipProps = React.HTMLAttributes<HTMLSpanElement> & VariantProps<typeof chip> & {
  /** A small leading dot in the tone colour (used for topics / tiers). */
  dot?: boolean;
};

/** A small labelled token: a flag, a topic, a tier, a count. Tone carries meaning, never decoration. */
export function Chip({ className, tone, interactive, dot, children, ...props }: ChipProps) {
  return (
    <span className={cn(chip({ tone, interactive }), className)} {...props}>
      {dot && <span className="inline-block size-1.5 rounded-full bg-current" aria-hidden />}
      {children}
    </span>
  );
}
