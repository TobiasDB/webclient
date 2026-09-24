import * as React from "react";
import * as Popover from "@radix-ui/react-popover";
import { Chip } from "../primitives/Chip";
import type { Flag } from "../types";

const TONE: Record<string, "bad" | "warn" | "accent" | "neutral" | "ok"> = {
  anti_bot_triggered: "bad", login_required: "bad", spa: "warn", anti_bot_present: "warn",
  login_present: "warn", pagination: "accent", tabbed: "accent", large_document: "neutral",
  shadow_dom: "accent", iframe: "accent", forms: "neutral", buttons: "neutral",
};

/** A flag as a chip; click for its evidence (the signals, stage, confidence, remedy). The
 * product's honesty rule: every conclusion shows what produced it (UX principle 1). */
export function FlagChip({ flag }: { flag: Flag }) {
  return (
    <Popover.Root>
      <Popover.Trigger asChild>
        <button type="button" className="inline-flex" aria-label={`${flag.name}: ${Math.round(flag.confidence * 100)}%`}>
          <Chip tone={TONE[flag.name] ?? "neutral"} interactive>
            {flag.name.replace(/_/g, " ")}
            <span className="opacity-70">{Math.round(flag.confidence * 100)}%</span>
          </Chip>
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content sideOffset={6} align="start"
          className="z-50 w-80 rounded-md border border-line bg-surface p-3 text-[12px] shadow-pop data-[state=open]:animate-in">
          <div className="mb-1 flex items-center justify-between">
            <span className="font-semibold text-ink">{flag.name}</span>
            {flag.remedy && <Chip tone="accent">remedy: {flag.remedy}</Chip>}
          </div>
          <ul className="flex flex-col gap-1.5">
            {flag.signals.map((s) => (
              <li key={s.name} className="rounded border border-line bg-surface-2 p-1.5">
                <div className="flex items-center justify-between">
                  <span className="font-mono text-ink">{s.name}</span>
                  <span className="text-muted">{s.stage} · {Math.round(s.confidence * 100)}%{s.contra ? " · contra" : ""}</span>
                </div>
                {s.reason && <div className="mt-0.5 text-muted">{s.reason}</div>}
              </li>
            ))}
            {!flag.signals.length && <li className="text-muted">No evidence fired.</li>}
          </ul>
          {flag.value != null && (
            <div className="mt-2 truncate font-mono text-[11px] text-muted" title={JSON.stringify(flag.value)}>
              value: {JSON.stringify(flag.value)}
            </div>
          )}
          <Popover.Arrow className="fill-line" />
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}

/** The present flags of a page as a row of chips, most actionable first (the flags() order). */
export function FlagRow({ flags, empty = "no flags" }: { flags: Flag[]; empty?: string }) {
  const present = flags.filter((f) => f.present);
  if (!present.length) return <span className="text-[12px] text-muted">{empty}</span>;
  return <div className="flex flex-wrap gap-1">{present.map((f) => <FlagChip key={f.name} flag={f} />)}</div>;
}
