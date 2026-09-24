import * as React from "react";
import { Chip } from "../primitives/Chip";
import type { WebError } from "../types";

/** One ledger entry: code · remedy · op/subject · message · hint (story 7.2). The remedy is
 * the thing an agent (or a person) acts on, so it is the most prominent chip. */
export function ErrorCard({ error, raised, when, onJump }: { error: WebError; raised?: boolean; when?: string; onJump?: () => void }) {
  return (
    <div className="rounded-md border-l-2 border-bad bg-surface p-2.5 text-[12px]">
      <div className="flex flex-wrap items-center gap-1.5">
        <code className="font-mono font-semibold text-bad">{error.code || error.type}</code>
        {error.remedy && <Chip tone="accent">remedy · {error.remedy}</Chip>}
        {error.retriable && <Chip tone="ok">retriable</Chip>}
        <Chip tone={raised ? "bad" : "neutral"}>{raised ? "raised" : "recorded"}</Chip>
        <span className="ml-auto text-muted">{when}</span>
      </div>
      <div className="mt-1 text-ink">{error.message}</div>
      <div className="mt-1 flex flex-wrap items-center gap-2 text-muted">
        {error.op && <span>op <code className="font-mono text-ink-2">{error.op}</code></span>}
        {error.subject && <span>on <code className="font-mono text-ink-2">{error.subject}</code></span>}
        {error.status_code ? <span>status {error.status_code}</span> : null}
        {onJump && <button type="button" onClick={onJump} className="ml-auto text-accent hover:underline">jump to moment →</button>}
      </div>
      {error.hint && <div className="mt-1 text-muted">{error.hint}</div>}
      {error.cause && <div className="mt-1 border-t border-line pt-1 text-muted">cause: <code className="font-mono">{error.cause.code || error.cause.type}</code> {error.cause.message}</div>}
    </div>
  );
}
