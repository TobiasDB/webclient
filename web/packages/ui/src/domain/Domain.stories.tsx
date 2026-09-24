import type { Meta, StoryObj } from "@storybook/react-vite";
import { FlagRow } from "./FlagChip";
import { TierLadder } from "./TierLadder";
import { ErrorCard } from "./ErrorCard";
import { AskCard } from "./AskCard";
import { StageRail } from "./StageRail";
import type { Flag } from "../types";

const meta: Meta = { title: "Domain/Findings & decisions" };
export default meta;

const FLAGS: Flag[] = [
  { name: "spa", present: true, confidence: 0.84, remedy: "browser", value: ["/api/items"], signals: [
    { name: "empty_root_shell", flag: "spa", stage: "static", confidence: 0.6, reason: "an empty #app container a bundle fills" },
    { name: "framework_marker", flag: "spa", stage: "static", confidence: 0.6, reason: "a react marker in the served HTML" },
  ] },
  { name: "pagination", present: true, confidence: 0.7, signals: [{ name: "rel_next_link", flag: "pagination", stage: "static", confidence: 0.7, reason: 'a rel="next" link' }], value: "/lab/paginated?page=2" },
  { name: "login_present", present: false, confidence: 0, signals: [] },
];

export const Flags: StoryObj = { render: () => <FlagRow flags={FLAGS} /> };

export const Ladder: StoryObj = {
  render: () => (
    <div className="flex flex-col gap-4">
      <TierLadder escalation={["static"]} timing={{ static: 42 }} />
      <TierLadder escalation={["static", "browser"]} timing={{ static: 38, browser: 910 }} reasons={{ browser: "spa" }} />
      <TierLadder escalation={["static", "proxy", "browser"]} reasons={{ proxy: "anti-bot", browser: "stealth" }} />
      <TierLadder escalation={["static"]} ask={{ reason: "render?", options: ["browser"] }} onEscalate={(t) => alert(t)} />
    </div>
  ),
};

export const Ledger: StoryObj = {
  render: () => (
    <div className="flex max-w-xl flex-col gap-2">
      <ErrorCard raised when="12:04:11" error={{ type: "LookupError", code: "select.no_match", message: "no match for '.nope'", remedy: "fix_selector", op: "select", subject: "doc:000-002", hint: "No element matches the selector; re-read the skeleton and pick a selector it shows." }} onJump={() => {}} />
      <ErrorCard error={{ type: "BrowserError", code: "fetch.browser_failed", message: "TimeoutError: page.goto", remedy: "retry", retriable: true, op: "fetch", subject: "doc:000-009", cause: { type: "TransportError", code: "fetch.transport", message: "connection reset" } }} />
    </div>
  ),
};

export const Ask: StoryObj = {
  render: () => (
    <div className="max-w-xl">
      <AskCard from="crawl" kind="crawl" ask={{ reason: "which frontier edges should I expand next?", options: ["/products", "/about", "/blog"] }} onAnswer={(a) => alert(String(a))}>
        <div className="text-muted">3 edges ranked · 12 pages fetched · budget 20</div>
      </AskCard>
    </div>
  ),
};

export const Rail: StoryObj = {
  render: () => (
    <StageRail selected="evaluate" onSelect={() => {}} stages={[
      { name: "search", status: "done", gate: "passed", summary: "3 seeds" }, { name: "crawl", status: "done", review: "good" },
      { name: "select", status: "done", gate: "passed" }, { name: "evaluate", status: "done", gate: "passed" },
      { name: "confirm", status: "waiting" }, { name: "source", status: "pending" }, { name: "query", status: "pending" },
    ]} />
  ),
};
