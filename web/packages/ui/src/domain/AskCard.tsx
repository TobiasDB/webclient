import * as React from "react";
import { Button } from "../primitives/Button";
import { Input } from "../primitives/Input";
import { Chip } from "../primitives/Chip";
import type { Ask } from "../types";

export type AskCardProps = {
  ask: Ask;
  /** Who is asking (a loop / pipeline name) and what kind of answer it takes. */
  from?: string;
  kind?: "crawl" | "resolve" | "pipeline" | string;
  onAnswer: (answer: unknown) => void;
  busy?: boolean;
  /** Show the context detail (the candidate picks, the assessment). */
  children?: React.ReactNode;
};

/** A waiting Ask is a DECISION CARD, never a spinner (UX principle 4): the reason, the
 * offered options as buttons, a custom answer, and the context below. */
export function AskCard({ ask, from, kind, onAnswer, busy, children }: AskCardProps) {
  const [custom, setCustom] = React.useState("");
  const options = (ask.options ?? []).slice(0, 12);
  return (
    <div className="rounded-lg border border-warn bg-warn-soft/40 p-3">
      <div className="mb-1 flex items-center gap-2">
        <Chip tone="warn" dot>waiting</Chip>
        {from && <span className="text-[12px] text-muted">{from}{kind ? ` · ${kind}` : ""}</span>}
      </div>
      <div className="text-[14px] font-medium text-ink">{ask.reason}</div>
      {children && <div className="mt-2 rounded-md border border-line bg-surface p-2 text-[12px]">{children}</div>}
      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        {options.map((o, i) => (
          <Button key={i} size="sm" variant={i === 0 ? "primary" : "secondary"} disabled={busy} onClick={() => onAnswer(o)}>
            {String(o)}
          </Button>
        ))}
        <form className="flex items-center gap-1" onSubmit={(e) => { e.preventDefault(); if (custom.trim()) onAnswer(custom.trim()); }}>
          <Input value={custom} onChange={(e) => setCustom(e.target.value)} placeholder="custom answer" className="h-7 w-44" />
          <Button size="sm" type="submit" disabled={busy || !custom.trim()}>resume</Button>
        </form>
      </div>
    </div>
  );
}
