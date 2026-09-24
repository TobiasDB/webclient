import * as React from "react";
import { cn } from "../lib/cn";
import type { Tier } from "../types";

const TIERS: Tier[] = ["static", "proxy", "browser"];
const COLOR: Record<Tier, string> = { static: "text-tier-static", proxy: "text-tier-proxy", browser: "text-tier-browser" };

export type TierLadderProps = {
  /** The tiers actually taken, in order (Transport.escalation). */
  escalation: Tier[];
  /** Why each hop was taken (by tier), shown under the step. */
  reasons?: Partial<Record<Tier, string>>;
  /** Timing per hop in ms, when known. */
  timing?: Partial<Record<Tier, number>>;
  /** A pending Ask on this document (manual mode): the next tier is offered, not taken. */
  ask?: { reason: string; options?: unknown[] } | null;
  onEscalate?: (tier: Tier) => void;
};

/** The transport ladder with the hops that were taken lit -- "cheapest tier that works" as a
 * picture (story 1.3). Under an Ask the next rung is a button (manual escalation). */
export function TierLadder({ escalation, reasons, timing, ask, onEscalate }: TierLadderProps) {
  const taken = new Set(escalation);
  const last = escalation[escalation.length - 1];
  return (
    <div className="flex items-center gap-2">
      {TIERS.map((t, i) => {
        const lit = taken.has(t);
        const offered = !!ask && !lit && (ask.options ?? []).includes(t);
        return (
          <React.Fragment key={t}>
            {i > 0 && <span className={cn("h-px w-6", lit ? "bg-ink-2" : "bg-line")} aria-hidden />}
            <div className="flex flex-col items-start">
              {offered ? (
                <button type="button" onClick={() => onEscalate?.(t)}
                  className={cn("rounded-full border border-dashed px-2.5 py-0.5 text-[12px] font-medium hover:bg-surface-2", COLOR[t])}>
                  {t} →
                </button>
              ) : (
                <span className={cn("rounded-full border px-2.5 py-0.5 text-[12px] font-medium",
                  lit ? cn("border-current bg-surface", COLOR[t]) : "border-line text-muted line-through decoration-line-2")}>
                  {t}{t === last && lit ? " ✓" : ""}
                </span>
              )}
              <span className="mt-0.5 h-3 text-[10px] text-muted">
                {timing?.[t] != null && lit ? `${timing[t]} ms` : ""}{reasons?.[t] && lit ? ` · ${reasons[t]}` : ""}
              </span>
            </div>
          </React.Fragment>
        );
      })}
      {ask && <span className="ml-2 rounded bg-warn-soft px-2 py-0.5 text-[11px] text-warn">waiting: {ask.reason}</span>}
    </div>
  );
}
