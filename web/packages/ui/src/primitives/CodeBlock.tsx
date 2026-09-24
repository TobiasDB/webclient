import * as React from "react";
import { Check, Copy } from "lucide-react";
import { cn } from "../lib/cn";

export type CodeBlockProps = {
  code: string;
  /** Shown top-right as a small label (e.g. "python", "http", "mcp"). */
  lang?: string;
  className?: string;
  /** Wrap long lines (default: scroll). */
  wrap?: boolean;
};

/** Monospace code with a copy button. No syntax highlighting on purpose (weight, and the DSL reads fine). */
export function CodeBlock({ code, lang, className, wrap }: CodeBlockProps) {
  const [copied, setCopied] = React.useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    } catch {
      /* clipboard unavailable: nothing to do */
    }
  };
  return (
    <div className={cn("group relative rounded-md border border-line bg-surface-2", className)}>
      <div className="absolute right-1.5 top-1.5 flex items-center gap-1">
        {lang && <span className="rounded bg-surface-3 px-1.5 text-[10px] uppercase tracking-wide text-muted">{lang}</span>}
        <button
          type="button"
          onClick={copy}
          aria-label="Copy"
          className="rounded p-1 text-muted opacity-0 transition-opacity hover:bg-surface-3 hover:text-ink group-hover:opacity-100 focus-visible:opacity-100"
        >
          {copied ? <Check size={14} /> : <Copy size={14} />}
        </button>
      </div>
      <pre className={cn("overflow-auto p-3 pr-16 font-mono text-[12px] leading-5 text-ink", wrap && "whitespace-pre-wrap break-words")}>
        <code>{code}</code>
      </pre>
    </div>
  );
}
