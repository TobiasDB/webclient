import * as React from "react";
import { Check, CircleDashed, Pause, X } from "lucide-react";
import { cn } from "../lib/cn";

export type StageStatus = "pending" | "running" | "done" | "failed" | "waiting" | "skipped";
export type StageInfo = { name: string; status: StageStatus; gate?: "passed" | "failed" | null; review?: string | null; summary?: string };

const ICON: Record<StageStatus, React.ReactNode> = {
  pending: <CircleDashed size={14} />, running: <span className="inline-block size-3 animate-pulse rounded-full bg-accent" />,
  done: <Check size={14} />, failed: <X size={14} />, waiting: <Pause size={14} />, skipped: <span className="text-[10px]">–</span>,
};
const TONE: Record<StageStatus, string> = {
  pending: "border-line text-muted", running: "border-accent text-accent", done: "border-ok text-ok",
  failed: "border-bad text-bad", waiting: "border-warn text-warn", skipped: "border-line text-muted",
};

/** A pipeline as a horizontal rail: one node per stage with its status, gate and review flag;
 * the selected stage expands below it (story 5.1). */
export function StageRail({ stages, selected, onSelect }: { stages: StageInfo[]; selected?: string; onSelect?: (name: string) => void }) {
  return (
    <ol className="flex items-start gap-0 overflow-x-auto py-1">
      {stages.map((s, i) => (
        <li key={s.name} className="flex items-start">
          {i > 0 && <span className={cn("mt-4 h-px w-8 shrink-0", s.status === "pending" ? "bg-line" : "bg-line-2")} aria-hidden />}
          <button type="button" onClick={() => onSelect?.(s.name)}
            className={cn("flex w-32 flex-col items-center gap-1 rounded-md p-1 text-center hover:bg-surface-2", selected === s.name && "bg-surface-2")}>
            <span className={cn("flex size-8 items-center justify-center rounded-full border-2 bg-surface", TONE[s.status])}>{ICON[s.status]}</span>
            <span className="text-[12px] font-medium text-ink">{s.name}</span>
            <span className="h-4 text-[10px] text-muted">
              {s.gate === "failed" ? "gate failed" : s.gate === "passed" ? "gate ✓" : s.review ? `review: ${s.review}` : s.summary ?? ""}
            </span>
          </button>
        </li>
      ))}
    </ol>
  );
}
