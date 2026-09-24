/** The wire types the components consume -- mirrors the service's JSON (webclient.models,
 * webclient.errors, webclient.patterns, the /traces and /loops endpoints). Kept minimal:
 * `[k: string]: unknown` where the Python side is open. */

export type Topic =
  | "network" | "network.navigation" | "dom" | "dom.update" | "action" | "console" | "plan"
  | "error" | "loop" | "pipeline" | "script" | "snapshot" | "resource" | "rrweb" | (string & {});

export interface Event {
  topic: Topic;
  version?: number;
  source?: string;
  n?: number;
  seq?: number;
  ts?: number;
  document_id?: string | null;
  session_id?: string | null;
  plan_id?: string | null;
  [k: string]: unknown;
}

export type Remedy =
  | "retry" | "browser" | "proxy" | "stealth" | "credentials" | "fix_selector" | "fix_plan"
  | "reproduce" | "reduce_load" | "unsupported" | "none";

export interface WebError {
  type: string;
  code?: string;
  title?: string;
  message: string;
  status_code?: number;
  retriable?: boolean;
  remedy?: Remedy | null;
  hint?: string;
  op?: string;
  subject?: string;
  cause?: WebError | null;
}

export interface ErrorEvent extends Event { topic: "error"; error: WebError; raised: boolean }

export type Stage = "request" | "static" | "rendered" | "network";

export interface Signal { name: string; flag: string; stage: Stage; confidence: number; contra?: boolean; reason?: string; value?: unknown }

export interface Flag { name: string; present: boolean; confidence: number; signals: Signal[]; remedy?: string | null; value?: unknown }

export type Tier = "static" | "proxy" | "browser";

export interface Transport { final_url?: string; final_tier: Tier; escalation: Tier[]; status_code?: number; [k: string]: unknown }

export interface PageCard {
  url: string; final_url?: string; kind: "html" | "json" | "xml" | "binary"; status_code?: number;
  title?: string | null; description?: string | null; flags: string[]; final_tier: Tier; escalation: Tier[];
}

export interface PatternHint {
  name: string; kind: "dom" | "visual" | "behavior" | "fingerprint"; subject: string; count: number;
  confidence: number; for_: Array<"extract" | "interact" | "crawl">; evidence?: string; value?: unknown;
}

export interface IndexedElement { index: number; role: string; name: string; kind: "interactive" | "content"; selector: string; repeats: number }

export interface Ask { reason: string; options?: unknown[]; detail?: Record<string, unknown> }

export interface LoopEvent extends Event {
  topic: "loop"; loop: string; round: number;
  phase: "round" | "decision" | "done" | "stalled" | "budget" | "error" | "waiting" | "resumed";
  detail: Record<string, unknown>;
}

export interface PipelineEvent extends Event {
  topic: "pipeline"; pipeline: string; stage: string; phase: "enter" | "exit" | "gate" | "review" | "error";
  detail: Record<string, unknown>;
}

export interface SnapshotEvent extends Event {
  topic: "snapshot"; phase: "fetch" | "load" | "action"; url: string; final_url?: string; kind: string;
  status_code: number; headers?: Record<string, string>; asset?: string; tiers?: Tier[];
}

export interface TraceSummary { id: string; events: number; started?: number; finished?: number; schema_version?: number }

export interface WaitingLoop { id: string; kind: "crawl" | "resolve"; ask: Ask }

export interface ToolSpec {
  name: string; description: string; input_schema: { properties?: Record<string, JsonSchema>; required?: string[] };
  returns?: string; story?: string; aliases?: string[];
}

export interface JsonSchema { type?: string | string[]; description?: string; default?: unknown; anyOf?: JsonSchema[]; enum?: unknown[]; items?: JsonSchema; [k: string]: unknown }

export interface Edge { url: string; text?: string; depth: number; score: number }

export interface Row { [column: string]: unknown }

/** The root of a dotted topic ("network.navigation" -> "network"). */
export const topicRoot = (t: string): string => t.split(".")[0] ?? t;
