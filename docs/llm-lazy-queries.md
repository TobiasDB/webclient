# Building lazy extraction queries (an LLM guide)

The query-syntax guide now ships **inside the package** as a skill, so an agent can
load it at runtime rather than reading a doc in the repo. It is deliberately scoped
to the **query syntax only** — the `wq` roots, `select` / `select_all` / `attr` /
`text_content`, `extract` / `filter` / `project`, the operators, and portable
blobs. It says nothing about how a page is fetched, rendered, or which client runs
the plan, because none of that is needed to *write* a query.

Get it any of these ways:

```python
from webclient import lazy_query_guide
print(lazy_query_guide())          # the skill text
```

- **MCP:** call the `lazy_query_guide` tool (no arguments).
- **File:** `webclient/skills/lazy-queries.md` (packaged data; skill frontmatter + body).

For the surrounding workflow that *produces* the selectors a query uses — reading a
page's token-lean **skeleton** (an HTML-tag outline, with `[xhr]`/`[js]` origin
marks on SPA pages) and the browser/probe tiers — see the **Select and extract** and
**Summary** sections of the [README](../README.md).
