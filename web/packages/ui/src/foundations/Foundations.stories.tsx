import type { Meta, StoryObj } from "@storybook/react-vite";
import { TopicChip } from "../domain/TopicChip";
import { Chip } from "../primitives/Chip";

const meta: Meta = { title: "Foundations/Tokens" };
export default meta;

const SEMANTIC = ["surface", "surface-2", "surface-3", "ink", "ink-2", "muted", "line", "line-2", "accent", "accent-soft", "ok", "ok-soft", "warn", "warn-soft", "bad", "bad-soft"];
const TOPICS = ["network", "network.navigation", "dom.update", "action", "console", "plan", "loop", "pipeline", "error", "snapshot", "script", "resource", "rrweb"];
const TIERS = ["static", "proxy", "browser"] as const;

export const Colours: StoryObj = {
  render: () => (
    <div className="flex flex-col gap-6">
      <section>
        <h3 className="mb-2 text-[12px] font-semibold uppercase tracking-wide text-muted">Semantic</h3>
        <div className="grid grid-cols-8 gap-2">
          {SEMANTIC.map((n) => (
            <div key={n} className="rounded-md border border-line p-1">
              <div className="h-10 rounded" style={{ background: `var(--color-${n})` }} />
              <div className="mt-1 font-mono text-[10px] text-muted">{n}</div>
            </div>
          ))}
        </div>
      </section>
      <section>
        <h3 className="mb-2 text-[12px] font-semibold uppercase tracking-wide text-muted">Event topics (one colour everywhere)</h3>
        <div className="flex flex-wrap gap-3">{TOPICS.map((t) => <TopicChip key={t} topic={t} />)}</div>
      </section>
      <section>
        <h3 className="mb-2 text-[12px] font-semibold uppercase tracking-wide text-muted">Transport tiers</h3>
        <div className="flex gap-3">{TIERS.map((t) => <span key={t} className="rounded-full border px-2.5 py-0.5 text-[12px]" style={{ color: `var(--color-tier-${t})`, borderColor: "currentColor" }}>{t}</span>)}</div>
      </section>
      <section>
        <h3 className="mb-2 text-[12px] font-semibold uppercase tracking-wide text-muted">Chip tones (meaning, not decoration)</h3>
        <div className="flex gap-2">
          <Chip tone="neutral">neutral</Chip><Chip tone="accent">accent · a hint / a remedy</Chip><Chip tone="ok">ok · passed</Chip>
          <Chip tone="warn">warn · needs attention / waiting</Chip><Chip tone="bad">bad · blocked / raised</Chip>
        </div>
      </section>
    </div>
  ),
};

export const Typography: StoryObj = {
  render: () => (
    <div className="flex flex-col gap-3">
      <div className="text-[20px] font-semibold">Screen title — 20/600</div>
      <div className="text-[14px] font-medium">Section / card title — 14/500</div>
      <div className="text-[13px]">Body — 13/400. The default for panels, tables and findings.</div>
      <div className="text-[12px] text-muted">Meta — 12/400 muted. Timestamps, evidence, hints.</div>
      <div className="text-[12px] font-semibold uppercase tracking-wide text-muted">Panel header — 12/600 caps</div>
      <div className="font-mono text-[12px]">mono 12 — code, selectors, topics, the skeleton</div>
    </div>
  ),
};
