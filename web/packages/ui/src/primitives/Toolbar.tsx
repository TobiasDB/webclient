import * as React from "react";
import { cn } from "../lib/cn";

/** A horizontal strip of controls; the top of every workspace. */
export function Toolbar({ className, children, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div className={cn("flex h-11 shrink-0 items-center gap-2 border-b border-line bg-surface px-3", className)} {...props}>
      {children}
    </div>
  );
}

export function ToolbarGroup({ className, children }: { className?: string; children: React.ReactNode }) {
  return <div className={cn("flex items-center gap-1", className)}>{children}</div>;
}

export function ToolbarSpacer() {
  return <div className="flex-1" />;
}
