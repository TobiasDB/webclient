import * as React from "react";
import { cn } from "../lib/cn";
import { topicRoot } from "../types";

const CLASS: Record<string, string> = {
  network: "text-topic-network", dom: "text-topic-dom", action: "text-topic-action", console: "text-topic-console",
  plan: "text-topic-plan", loop: "text-topic-loop", pipeline: "text-topic-pipeline", error: "text-topic-error",
  snapshot: "text-topic-snapshot", script: "text-topic-script", resource: "text-topic-resource", rrweb: "text-topic-rrweb",
};

/** A topic coloured consistently everywhere (timeline lanes, chips, the run bar). */
export function TopicChip({ topic, className, dot = true }: { topic: string; className?: string; dot?: boolean }) {
  const root = topicRoot(topic);
  return (
    <span className={cn("inline-flex items-center gap-1 font-mono text-[11px] font-semibold", CLASS[root] ?? "text-ink-2", className)}>
      {dot && <span className="inline-block size-1.5 rounded-full bg-current" aria-hidden />}
      {topic}
    </span>
  );
}

export const topicColorVar = (topic: string) => `var(--color-topic-${topicRoot(topic)}, var(--color-ink-2))`;
