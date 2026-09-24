import type { Meta, StoryObj } from "@storybook/react-vite";
import * as React from "react";
import { Search } from "lucide-react";
import { Button } from "./Button";
import { Chip } from "./Chip";
import { Panel } from "./Panel";
import { Tabs, TabPanel } from "./Tabs";
import { CodeBlock } from "./CodeBlock";
import { EmptyState } from "./EmptyState";
import { KeyValue } from "./KeyValue";
import { Toolbar, ToolbarGroup, ToolbarSpacer } from "./Toolbar";
import { Input, Select } from "./Input";

const meta: Meta = { title: "Primitives/All" };
export default meta;

export const Buttons: StoryObj = {
  render: () => (
    <div className="flex flex-wrap items-center gap-2">
      <Button variant="primary">Run</Button><Button>Secondary</Button><Button variant="ghost">Ghost</Button>
      <Button variant="danger">Close session</Button><Button size="sm">Small</Button><Button size="lg" variant="primary">Large</Button>
      <Button disabled>Disabled</Button>
    </div>
  ),
};

export const Chips: StoryObj = {
  render: () => (
    <div className="flex gap-2"><Chip>3 rows</Chip><Chip tone="accent" dot>network</Chip><Chip tone="ok">tested</Chip><Chip tone="warn">waiting</Chip><Chip tone="bad">raised</Chip><Chip interactive tone="accent">clickable</Chip></div>
  ),
};

export const PanelAndTabs: StoryObj = {
  render: function Story() {
    const [tab, setTab] = React.useState("a");
    return (
      <div className="grid h-72 grid-cols-2 gap-3">
        <Panel title="Findings" actions={<Button size="sm" variant="ghost">↻</Button>}>
          <KeyValue rows={[["title", "Roasters Coffee"], ["kind", "html"], ["tier", "static"], ["flags", <Chip tone="accent">pagination</Chip>]]} />
        </Panel>
        <Panel title="Detail" flush>
          <Tabs items={[{ value: "a", label: "Event" }, { value: "b", label: "Snapshot" }, { value: "c", label: "Errors", count: 2 }]} value={tab} onValueChange={setTab} className="h-full">
            <TabPanel value="a" className="p-3">Event JSON</TabPanel>
            <TabPanel value="b" className="p-3">Snapshot</TabPanel>
            <TabPanel value="c" className="p-3">Errors</TabPanel>
          </Tabs>
        </Panel>
      </div>
    );
  },
};

export const Code: StoryObj = {
  render: () => (
    <div className="flex flex-col gap-3">
      <CodeBlock lang="python" code={`with WebClient() as wc:\n    page = wc.fetch("https://example.com", browser="auto")\n    rows = page.select_all("div.card").extract(title=doc.select(".title").attr("text")).project()`} />
      <CodeBlock lang="http" code={`POST /tools/extract\n{"url": "https://example.com", "result": "div.card", "fields": {"title": ".title"}}`} wrap />
    </div>
  ),
};

export const Empty: StoryObj = {
  render: () => (
    <div className="h-64 rounded-lg border border-line">
      <EmptyState icon={<Search size={28} />} title="Start with a URL" hint="Paste any page. You'll see what it is, what's notable, and where the data is." action={<Button variant="primary">Explore</Button>} />
    </div>
  ),
};

export const ToolbarStory: StoryObj = {
  name: "Toolbar",
  render: () => (
    <Toolbar>
      <ToolbarGroup><Input placeholder="https://…" className="w-96" mono /></ToolbarGroup>
      <ToolbarGroup><Select defaultValue="auto"><option>static</option><option>auto</option><option>always</option></Select></ToolbarGroup>
      <ToolbarSpacer />
      <Button variant="primary" size="sm">Explore</Button>
    </Toolbar>
  ),
};
