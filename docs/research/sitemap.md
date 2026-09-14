# Case study: making `sitemap` / site-map (URL discovery & mapping) best-in-class

*Scope:* how the `sitemap` / "map this site" feature of this web-client should work to be
maximally useful to LLMs/agents and end users. Optimises for a clean, deduped,
categorized, bounded URL list with light metadata, delivered fast.

*Method:* full read of the current implementation
(`webclient/core/client/__init__.py`, `webclient/core/crawl/*`,
`webclient/core/document/summary.py`, `webclient/core/document/models.py`,
`webclient/service.py`, `demo.py`) plus the sitemaps.org protocol and the leading
LLM/agent web tools (Firecrawl `map`, Tavily, Screaming Frog, Scrapy/Crawlee).
No code was changed — this is a design note. Sources at the end.

---

## 1. State of the art

### 1.1 The `sitemap.xml` protocol (what "the real sitemap" is)

The [Sitemaps 0.9 protocol](https://www.sitemaps.org/protocol.html) is a machine-readable
list of a site's canonical URLs the site *itself* publishes — no crawling required:

- **`<urlset>`** — a leaf sitemap. Each `<url>` has a required **`<loc>`** (the URL) and
  optional **`<lastmod>`** (ISO-8601 date — the one field Google actually trusts),
  **`<changefreq>`** (`always`…`never`, a hint), and **`<priority>`** (0.0–1.0, a hint).
- **`<sitemapindex>`** — a sitemap *of sitemaps*. Each `<sitemap>` has a `<loc>` pointing
  at another sitemap (leaf or index) plus an optional `<lastmod>`. Large sites fan out
  through an index into dozens of child sitemaps.
- **Limits:** each file ≤ **50,000 URLs** and ≤ **50 MB** uncompressed; beyond either,
  the site splits and uses an index. Files are commonly **gzip-compressed** (`.xml.gz`).
- **Extensions:** namespaced **news**, **image**, and **video** sitemaps add per-URL
  metadata (publication date, image loc, video duration, …).

**Discovery** of the sitemap(s) themselves, in priority order:

1. **`robots.txt` `Sitemap:` lines** — the canonical advertisement; absolute URLs, one per
   line, may point anywhere (even a different host/CDN).
2. **Well-known paths** — `/sitemap.xml`, then `/sitemap_index.xml`, `/sitemap-index.xml`,
   `/wp-sitemap.xml`, `/sitemap/`, etc.
3. **`<link rel="sitemap">`** in the homepage `<head>` (rare but authoritative).
4. **RSS/Atom feeds** (`<link rel="alternate" type="application/rss+xml">`) — a secondary
   URL source for the freshest content.

### 1.2 How leading tools map a site

- **Firecrawl `/map`** — the reference "map this site" endpoint. Returns *URLs only*
  (no page bodies) in **~2–3 s**, up to ~5k–100k links per request. It **fuses sources**:
  the site's **sitemap.xml first**, supplemented by **search-engine/SERP results** and its
  **crawl-index cache**, then dedups. A **`search=` parameter** filters the map to URLs
  relevant to a topic ("map --search pricing"), and the canonical agent pattern is
  **map → pick URL → scrape** (cheap discovery, then targeted extraction).
- **Tavily `map`/`crawl`** — same split: a fast graph/URL map distinct from content crawl.
- **Screaming Frog / Sitebulb (site-audit tools)** — hybrid: parse XML sitemap(s) *and*
  crawl links, then reconcile (URLs in sitemap but not crawlable = orphans; crawlable but
  not in sitemap = missing from sitemap). They canonicalize and de-duplicate aggressively.
- **Scrapy `SitemapSpider` / Crawlee** — seed the frontier directly from `robots.txt`
  `Sitemap:` + `sitemap.xml`, transparently follow `<sitemapindex>` recursion and gunzip
  `.gz`, and expose `lastmod` for incremental crawls.

**Consensus shape of a good "map":** *seed from the site's own sitemap + robots, optionally
top up with a bounded link crawl, canonicalize + dedup, and return a flat, categorized URL
list with light metadata (lastmod), fast and URL-only by default.*

### 1.3 What an LLM/agent actually wants from "map this site"

- A **clean, deduped, canonical URL list** — not page dumps (tokens are precious).
- **Fast and bounded** — a couple of seconds, a known cap, no surprise 10-minute crawl.
- **Categorized/grouped** — by path section (`/docs/*`, `/blog/*`, `/api/*`) and/or page
  type, so the agent can pick a subset without reading everything.
- **Light metadata per URL** — at least `lastmod` (for "what changed since?" and
  incremental fetches) and the discovery source.
- **Searchable/filterable** — "give me only the pricing/docs URLs" (Firecrawl `search=`).
- **A cheap first step** — map first, then `fetch`/`extract` only the URLs that matter.

---

## 2. Our current implementation (honest assessment)

**`wc.sitemap(url)` does not parse the sitemap at all — it is an eager link crawl.**

- `WebClient.sitemap(...)` (`webclient/core/client/__init__.py:488`) is literally
  `self.crawl(url, auto=True, depth=2, width=20, max_pages=1000).run()` — a single-domain,
  best-first auto crawl run to completion.
- The crawl frontier is expanded by `CrawlBacking._expand`
  (`webclient/core/crawl/backing.py:103`): it fetches each page and reads **`a[href]`
  anchors only**, splits off `#fragments`, filters same-origin + include/exclude, and dedups
  by **exact URL string** in `core._seen`.
- Each fetched page becomes a `Summary` in `crawl.pages`; unresolved edges stay in
  `crawl.frontier` (`webclient/core/crawl/models.py` — `ICrawl`, `Edge`). The service
  `/sitemap` endpoint (`webclient/service.py:323`) returns `{pages, urls, frontier, done}`,
  where `urls` is `[p.transport.final_url for p in crawl.pages]`
  (`_crawl_response`, `service.py:282`).

### Gaps (concrete)

1. **Ignores the real `sitemap.xml`.** Nothing fetches `/sitemap.xml`, follows a
   `<sitemapindex>`, gunzips `.gz`, or reads `<loc>`/`<lastmod>`. Worse: `_expand` only reads
   `a[href]`, so even if the crawl *did* fetch a `sitemap.xml` (it's `kind="xml"`, which
   `step` allows at `backing.py:75`), it would extract **zero** URLs — a `<urlset>` has
   `<loc>` elements, not anchors.
2. **Ignores `robots.txt` `Sitemap:` directives.** `_load_robots`
   (`backing.py:129`) already fetches `robots.txt` and builds a `RobotFileParser` — but only
   calls `can_fetch`. `RobotFileParser.site_maps()` (the `Sitemap:` URLs) is right there and
   unused.
3. **Dead detection fields.** `Metadata.sitemap_url`
   (`webclient/core/document/models.py:98`) is declared but **never assigned anywhere**
   (confirmed by grep — `summary.py:metadata()` builds `Metadata(...)` without it).
   `Metadata.feeds` **is** detected (`summary.py:153`, RSS/Atom `<link>`s) but **never
   consumed** by the crawl/sitemap path.
4. **No fast, URL-only mode.** Mapping requires fetching and summarising *every* page
   (up to `max_pages=1000`) — minutes and megabytes where Firecrawl `map` returns URLs in
   seconds. There is no "just the URLs" path.
5. **No canonicalization.** Dedup is exact-string on the fragment-stripped URL
   (`backing.py:108`). `http`/`https`, trailing slash, default ports, `index.html`, and
   `utm_*`/tracking query params all produce *distinct* frontier entries → duplicate pages.
6. **No `lastmod`/`priority`/`changefreq`.** `Edge` (`models.py:23`) carries only
   `url`/`text`/`depth`. There's nothing to drive incremental "what changed" fetches, and
   the best-first `_score` (`backing.py:94`) can't use sitemap `priority`.
7. **No categorization/grouping.** The output is a flat `pages`+`frontier`; nothing groups
   URLs by section or page type, so an agent must eyeball the list.
8. **Coverage is crawl-limited.** Orphan pages (in the sitemap, not linked) and
   JS-only-linked pages are invisible; the map is only as complete as the anchor graph.

**Net:** the feature is mis-named. It's a bounded auto-crawl (a fine "explore from a seed"),
but it is *not* a sitemap: it never consults the map the site publishes about itself.

---

## 3. Best-in-class target (what a fully implemented map/sitemap looks like here)

A two-mode feature, LLM/agent-first:

### Mode A — **`map` (fast, URL-only)** — the default an agent reaches for

1. **Discover sitemap sources** (no page fetches beyond tiny files):
   - fetch `robots.txt`, read `RobotFileParser.site_maps()` (`Sitemap:` lines);
   - fall back to well-known paths (`/sitemap.xml`, `/sitemap_index.xml`, …);
   - fetch the homepage once and read `<link rel="sitemap">` → populate
     `Metadata.sitemap_url`, and `Metadata.feeds` (RSS/Atom) as a supplementary source.
2. **Parse sitemaps:** recurse `<sitemapindex>` → child sitemaps, gunzip `.gz`, read each
   `<url>`'s `<loc>` + `<lastmod>`/`<changefreq>`/`<priority>`. Honour the 50k/50 MB caps and
   an overall URL budget.
3. **Optionally top up with a shallow link crawl** (depth 1–2) to catch pages missing from
   the sitemap — Screaming-Frog-style hybrid, reusing today's `_expand`.
4. **Canonicalize + dedup:** one canonical-URL key (lowercase scheme/host, strip default
   port + fragment + `utm_*`/tracking params, normalise trailing slash / `index.html`).
5. **Categorize:** group by path prefix (`/docs`, `/blog`, `/api`, …) and/or detected
   `page_type`; attach `lastmod` + `source` (sitemap | robots | feed | link) per URL.
6. **Return a flat, deduped, categorized URL list with light metadata** — fast (seconds),
   URL-only, bounded. Support a `search=`/`include=` filter ("only pricing/docs URLs").

### Mode B — **`sitemap`/`crawl` (content)** — today's behaviour, but *seeded*

The existing auto-crawl, but its frontier is **seeded from Mode-A discovery first** (so it
starts from the site's own canonical URLs, then follows links), canonicalizes its dedup key,
and carries `lastmod` so it can skip unchanged pages (incremental).

**Priorities for LLM/agent usefulness (highest first):** (1) seed from the real
sitemap.xml + robots `Sitemap:`; (2) a fast URL-only mode; (3) canonicalization/dedup;
(4) `lastmod`-aware output + incremental; (5) categorization/grouping; (6) `search=`/filter.

---

## 4. Concrete recommendations for THIS codebase

Keep the cores + backings architecture. The seams already exist; most of this is additive.

**The seam is `WebClient.sitemap()` (`client/__init__.py:488`) + a discovery step feeding
the crawl frontier + the already-present-but-unused `Metadata.sitemap_url`/`feeds`.**

Prioritised:

1. **Add a discovery step that seeds the frontier from the real sitemap + robots.**
   The cleanest home is a new `SitemapBacking` (mirroring `CrawlBacking`) or a `discover`
   method on `CrawlBacking`, invoked before the first `step`:
   - reuse `_load_robots` (`backing.py:129`) and call `RobotFileParser.site_maps()` — the
     `Sitemap:` URLs are already parsed and thrown away today;
   - probe `/sitemap.xml`, `/sitemap_index.xml` via the existing `core._client.afetch`;
   - parse `<sitemapindex>`/`<urlset>` (stdlib `xml.etree`), recursing indexes and
     gunzipping `.gz`; seed each `<loc>` as an `Edge(url=…, depth=0)` into `core.frontier`
     and `core._seen`. This makes `sitemap()` actually consult the sitemap while the rest of
     the crawl machinery (dedup, robots, scope, budget) is unchanged.

2. **Populate and consume the dead detection fields.**
   In `summary.py:metadata()`, set `Metadata.sitemap_url` from `<link rel="sitemap">` and/or
   the robots `Sitemap:` line (it's declared at `models.py:98` and never assigned). Feed
   `Metadata.feeds` (already detected, `summary.py:153`) into discovery as a supplementary
   source. This closes the "detected but unused" gap the models call out.

3. **Add a fast, URL-only `map` mode.** Give `WebClient.sitemap(...)` a `fast=True`
   (or a sibling `map()` verb) that runs discovery + canonicalize + categorize and returns a
   URL list **without** fetching/summarising each page. Wire a `mode`/`fast` param through the
   `/sitemap` endpoint (`service.py:323`); the response already carries a flat `urls`
   (`_crawl_response`, `service.py:288`) — extend it with `lastmod` + `categories`.

4. **Canonicalize the dedup key.** Add a `_canonical(url)` helper and use it as the
   `core._seen` key + `Edge` identity in `_expand` (`backing.py:108`) and discovery:
   lowercase scheme/host, drop default port + fragment (already stripped) + tracking params,
   normalise trailing slash. Kills the http/https + trailing-slash + `utm_*` duplicates.

5. **Carry `lastmod`/`priority` on the frontier.** Extend `Edge` (`models.py:23`) with
   optional `lastmod`/`priority`/`source` (backward-compatible defaults). Let `_score`
   (`backing.py:94`) fold in `priority`, and expose `lastmod` in the output so agents fetch
   only changed URLs (incremental map). Add a small value model — e.g. `UrlEntry`/`SiteMap`
   (pure pydantic, like `Summary`/`Edge`) — for the URL-only result.

6. **Categorize the output.** Group returned URLs by leading path segment and/or the
   `page_type` already computed in `Metadata` (`summary.py:152`); return per-group counts.
   Low effort, high agent value.

7. **Add a `search=`/`include=` filter to the map** (Firecrawl parity) — the crawl already
   has `include`/`exclude`/`keywords` (`client/__init__.py:461`); expose the same on the map
   so "map --search pricing" works.

**Suggested phasing:** #1+#2 first (make `sitemap` actually a sitemap, zero new surface) →
#3+#4 (the fast URL-only, deduped map — the headline agent win) → #5+#6+#7 (lastmod,
categories, search — the polish that makes it best-in-class).

---

## 5. Sources

- [sitemaps.org — protocol (urlset/sitemapindex, lastmod/changefreq/priority, 50k/50MB, gzip)](https://www.sitemaps.org/protocol.html)
- [Google Search Central — Build and submit a sitemap (index files, `Sitemap:` in robots.txt, what Google honours)](https://developers.google.com/search/docs/crawling-indexing/sitemaps/build-sitemap)
- [Firecrawl — Map endpoint (fast URL discovery, sitemap + SERP + index, `search=`)](https://docs.firecrawl.dev/features/map)
- [Firecrawl — Launch Week: introducing the Map endpoint](https://www.firecrawl.dev/blog/launch-week-i-day-3-introducing-map-endpoint)
- [Firecrawl — How to generate a sitemap using the /map endpoint](https://www.firecrawl.dev/blog/how-to-generate-sitemap-using-firecrawl-map-endpoint)
- [Scrapy — `SitemapSpider` (robots `Sitemap:` + sitemap.xml + index recursion + gzip)](https://docs.scrapy.org/en/latest/topics/spiders.html#sitemapspider)
- [Python stdlib — `urllib.robotparser.RobotFileParser.site_maps()`](https://docs.python.org/3/library/urllib.robotparser.html)
- Repo (this codebase): `webclient/core/client/__init__.py:488` (`sitemap`), `webclient/core/crawl/backing.py` (`CrawlBacking._expand`/`_load_robots`), `webclient/core/crawl/models.py` (`Edge`/`ICrawl`), `webclient/core/document/models.py:97-98` (`Metadata.feeds`/`sitemap_url`), `webclient/core/document/summary.py:153` (feed detection), `webclient/service.py:282,323` (`_crawl_response`/`/sitemap`).
- Companion notes: `docs/assessment.md` (§ crawling gaps), `docs/llm-usability.md` (Firecrawl map/sitemap parity).
