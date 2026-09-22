# webclient

A declarative web client: fetch pages, select and extract structured data, render to
markdown, drive a real browser, and run the **same plan** synchronously, asynchronously, or
against a remote service. Everything the engine does is an **event** you can subscribe to,
**trace** to disk, and **replay** offline.

```python
from webclient import WebClient

with WebClient() as wc, wc.trace("run.trace"):
    page = wc.fetch("https://example.com", browser="auto")   # cheapest tier that works
    print(page.card().title, [f.name for f in page.flags()])
    rows = page.select_all(page.patterns(for_="extract")[0].subject).extract(
        title=wc.doc.select("h2").attr("text")).project()
```

- **Tools** — the high-level API, one registry for Python, MCP and HTTP: [tools.md](tools.md).
- **Errors** — catalogued, problem-details shaped, with a remedy: [errors.md](errors.md).
- **Signals & flags** — what the client detects and why: [signals.md](signals.md).
- **The lab** — `python -m webclient.lab` serves one fixture page per feature, each with its
  expected result; tests, demos and these docs assert against it.
- **API reference** — [api.md](api.md).

See the repository `README.md` for install and the full tour, and `Roadmap.md` for where
this is going.
