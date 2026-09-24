import * as React from "react";
import * as RT from "@radix-ui/react-tabs";
import { cn } from "../lib/cn";

export type TabItem = { value: string; label: React.ReactNode; count?: number };

export type TabsProps = {
  items: TabItem[];
  value: string;
  onValueChange: (value: string) => void;
  children: React.ReactNode;
  className?: string;
};

/** Underlined tabs (Radix). Children are `<TabPanel value>`s. */
export function Tabs({ items, value, onValueChange, children, className }: TabsProps) {
  return (
    <RT.Root value={value} onValueChange={onValueChange} className={cn("flex min-h-0 flex-col", className)}>
      <RT.List className="flex shrink-0 gap-4 border-b border-line px-3">
        {items.map((t) => (
          <RT.Trigger
            key={t.value}
            value={t.value}
            className="-mb-px flex h-9 items-center gap-1.5 border-b-2 border-transparent text-[13px] text-muted data-[state=active]:border-accent data-[state=active]:text-ink"
          >
            {t.label}
            {t.count != null && <span className="rounded-full bg-surface-3 px-1.5 text-[11px] text-ink-2">{t.count}</span>}
          </RT.Trigger>
        ))}
      </RT.List>
      {children}
    </RT.Root>
  );
}

export function TabPanel({ value, children, className }: { value: string; children: React.ReactNode; className?: string }) {
  return (
    <RT.Content value={value} className={cn("min-h-0 flex-1 overflow-auto outline-none", className)}>
      {children}
    </RT.Content>
  );
}
