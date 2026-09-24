import * as React from "react";
import { cn } from "../lib/cn";

/** LOW-FI WIREFRAME primitives. A screen is designed first as grey boxes with names and the
 * one-line purpose of each region, so layout, hierarchy and flow are agreed before a single
 * real component exists (docs/product/playground-user-stories.md §6). */

export function Frame({ title, children, className, width = 1280, height = 800 }: {
  title: string; children: React.ReactNode; className?: string; width?: number; height?: number;
}) {
  return (
    <div className={cn("relative overflow-hidden rounded-md border-2 border-dashed border-line-2 bg-surface-2 font-sans", className)}
         style={{ width, height }}>
      <div className="absolute left-2 top-1 rounded bg-surface-3 px-1.5 text-[10px] uppercase tracking-wider text-muted">wireframe · {title}</div>
      <div className="grid h-full w-full pt-5">{children}</div>
    </div>
  );
}

export function Box({ label, note, children, className, tone = "box", grow }: {
  label: string; note?: string; children?: React.ReactNode; className?: string;
  tone?: "box" | "nav" | "primary" | "muted"; grow?: boolean;
}) {
  return (
    <div className={cn(
      "m-1 flex min-h-8 flex-col rounded border p-2 text-[11px]",
      tone === "box" && "border-line-2 bg-surface text-ink-2",
      tone === "nav" && "border-line-2 bg-surface-3 text-ink-2",
      tone === "primary" && "border-accent bg-accent-soft text-accent",
      tone === "muted" && "border-dashed border-line-2 bg-transparent text-muted",
      grow && "flex-1", className,
    )}>
      <div className="font-semibold">{label}</div>
      {note && <div className="text-[10px] text-muted">{note}</div>}
      {children}
    </div>
  );
}

export function Row({ children, className, cols }: { children: React.ReactNode; className?: string; cols?: string }) {
  return <div className={cn("grid min-h-0 gap-0", className)} style={{ gridTemplateColumns: cols ?? `repeat(${React.Children.count(children)}, minmax(0,1fr))` }}>{children}</div>;
}

export function Col({ children, className, rows }: { children: React.ReactNode; className?: string; rows?: string }) {
  return <div className={cn("grid min-h-0 gap-0", className)} style={{ gridTemplateRows: rows ?? "auto" }}>{children}</div>;
}

export function Lines({ n = 4, w = "100%" }: { n?: number; w?: string }) {
  return (
    <div className="mt-1 flex flex-col gap-1">
      {Array.from({ length: n }).map((_, i) => (
        <div key={i} className="h-1.5 rounded bg-line" style={{ width: i === n - 1 ? "60%" : w }} />
      ))}
    </div>
  );
}

export function Pill({ children }: { children: React.ReactNode }) {
  return <span className="mr-1 inline-block rounded-full border border-line-2 px-1.5 text-[10px]">{children}</span>;
}
