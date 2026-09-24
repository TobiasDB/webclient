import * as React from "react";
import { cn } from "../lib/cn";

export type KeyValueProps = { rows: Array<[React.ReactNode, React.ReactNode]>; className?: string };

/** A compact label/value list (the "findings" style). */
export function KeyValue({ rows, className }: KeyValueProps) {
  return (
    <dl className={cn("grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-[13px]", className)}>
      {rows.map(([k, v], i) => (
        <React.Fragment key={i}>
          <dt className="text-muted">{k}</dt>
          <dd className="min-w-0 break-words text-ink">{v}</dd>
        </React.Fragment>
      ))}
    </dl>
  );
}
