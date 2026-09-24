# web/ — the WebClient web front ends

One npm workspace, three packages (kept deliberately simple):

| package | what | run |
|---|---|---|
| `packages/ui` | the shared component library + design tokens + **Storybook** (wireframes and components are both stories) | `npm run storybook` |
| `apps/playground` | the Playground (Vite + React + TS) — served by the service at `/playground` when built | `npm run dev:playground` |
| `apps/site` | the product website (Astro + React islands) — the site is also the lab | `npm run dev:site` |

Process (docs/product/*.md): user stories → Storybook → wireframes and components →
scenes. Storybook is the design tool: every screen exists first as a wireframe story,
then as real components, then is assembled into a scene in an app.

    npm install
    npm run storybook           # http://localhost:6006
    npm run dev:playground      # http://localhost:5173 (proxies /tools, /events, ... to the service on :8000)
    npm run dev:site            # http://localhost:4321

The apps talk only to the WebClient service (`make serve`); no auth in the Playground.
