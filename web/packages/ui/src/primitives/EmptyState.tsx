import * as React from "react";
import { cn } from "../lib/cn";

export type EmptyStateProps = {
  icon?: React.ReactNode;
  title: React.ReactNode;
  /** One sentence: what to do next, never "no data". */
  hint?: React.ReactNode;
  action?: React.ReactNode;
  className?: string;
};

/** Every screen's empty / zero state invites the next action (story 10.3). */
export function EmptyState({ icon, title, hint, action, className }: EmptyStateProps) {
  return (
    <div className={cn("flex h-full min-h-40 flex-col items-center justify-center gap-2 p-6 text-center", className)}>
      {icon && <div className="text-muted">{icon}</div>}
      <div className="text-[14px] font-medium text-ink">{title}</div>
      {hint && <div className="max-w-md text-[13px] text-muted">{hint}</div>}
      {action && <div className="mt-1">{action}</div>}
    </div>
  );
}
