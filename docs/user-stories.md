# User stories

Who uses webclient, what they need from it, and which **tools** (the registry in
`webclient/tools`, exposed identically over Python, MCP and `POST /tools/{name}`) serve
them. Every tool cites its story; a tool with no story does not get added.

## Analyst — "what does this page say?"

Reads pages, not DOMs. Wants a clean, small, trustworthy view of a URL and nothing to
configure.

- `fetch_markdown`, `fetch_text` — the page as readable text.
- `links` — where it leads.
- `card` — the one-line summary: title / description / flags / how it was fetched.

## Data engineer — "give me this dataset as rows, and keep it working"

Turns a site into a repeatable extraction: finds the dataset, writes the query, pages
through it, schedules it. Cares about durability (selectors that survive a redeploy) and
cost (static before browser).

- `patterns` — where the records are (`select_all` target), what repeats.
- `extract` — rows from a record selector + field selectors.
- `sitemap`, `robots`, `crawl` — discovering the pages that hold the data.
- The onboarding pipeline (`webclient.pipelines`) for the whole find-and-author flow, with
  `interactive=True` to confirm the chosen source.

## Agent developer — "let my model use the web safely"

Builds an LLM agent over the tools. Needs token-lean observations, typed errors with a
remedy, and a way to author plans the model can check before running.

- `skeleton` — the selector map an LLM writes CSS from.
- `flags` — what is notable / blocking (SPA, anti-bot, login, pagination) with remedies.
- `validate_plan`, `run_plan`, `lazy_query_guide` — author a plan, check it, run it.
- Every error is catalogued (`docs/errors.md`) with a `remedy` the agent can branch on.

## Operator — "run it for others, observe it, scale it"

Hosts the service, watches the stream, replays a run when something went wrong.

- `GET /health`, `GET /tools` — what is running and what it offers.
- `/events` (live, `since=` resume, `trace=` replay) and traces (`wc.trace`) — see
  everything the engine did; `Replay` to inspect it offline.
- `Settings` (`WEBCLIENT_*`) for every knob; `docs/signals.md` / `docs/errors.md` for
  what the system detects and how it fails.
