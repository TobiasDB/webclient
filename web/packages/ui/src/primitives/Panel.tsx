import * as React from "react";
import { cn } from "../lib/cn";

export type PanelProps = React.HTMLAttributes<HTMLElement> & {
  title?: React.ReactNode;
  /** Right-aligned controls in the header. */
  actions?: React.ReactNode;
  /** Remove the body padding (for tables / previews that fill the panel). */
  flush?: boolean;
};

/** A titled surface. Every workspace is panels in a grid; the title row is the only chrome. */
export function Panel({ title, actions, flush, className, children, ...props }: PanelProps) {
  return (
    <section className={cn("flex min-h-0 flex-col rounded-lg border border-line bg-surface", className)} {...props}>
      {(title || actions) && (
        <header className="flex h-9 shrink-0 items-center justify-between gap-2 border-b border-line px-3">
          <h2 className="truncate text-[12px] font-semibold uppercase tracking-wide text-muted">{title}</h2>
          <div className="flex items-center gap-1">{actions}</div>
        </header>
      )}
      <div className={cn("min-h-0 flex-1 overflow-auto", !flush && "p-3")}>{children}</div>
    </section>
  );
}
