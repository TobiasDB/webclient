# Pagination

Status: built in the package, 2026-09-26 (it replaces the `by=` pagination API; an old `by=` plan is refused with a hint). The UI follows.

## The idea

A pager is an **iterator** over the pages of one dataset, plus **until** (when to stop), **filter** (which pages to keep) and **bounds**. It is one `BoundedLoop`, the same one behind `doc.paginate(...)` (in a plan) and `wc.paginate(...)` (a session you can step through).

**Detection only hints.** `doc.pagination()` lists the ways a page could be paged, each with its evidence and a ready-to-copy expression. Nothing is ever run from a hint. You choose one, or an LLM does.

## Writing a pager

Give exactly one **iterator**:

| iterator | reads | example |
|---|---|---|
| `next=<Expr>` | each page gives the **next page's Reference**: a link, a text link, or the HTTP `Link` header. A string is a selector whose `href` is followed. No next page means the end. | `next=wq.doc.select("li.next a").attr("href")` · `next="li.next a"` · `next=wq.doc.next_link()` |
| `pages="<param>"` | an **integer iterator** over a URL parameter, from `start` (by default the value in the current URL, else 1) by `step` up to `stop` (the last value, included). `stop` is an int, or an Expr read off page one. | `pages="page"` · `pages="offset", start=0, step=20` · `pages="page", stop=wq.doc.select(".pager .last").attr("text").number()` |
| `cursor=<Expr>, param="<p>"` | a **token** read off each page, carried in `?p=`. No token means the end. | `cursor=wq.doc.select("a.next").attr("data-after"), param="after"` |
| `click=<selector \| Expr>` | on the held live page, **click** to load more until nothing more loads or the control is gone. A string is the control's selector; an Expr is the action to run. | `click="button.more"` · `click=wq.doc.click("button.more", timeout=2)` |
| `scroll=True` | on the held live page, **scroll** to load more (infinite scroll). | `scroll=True` |

Then, all optional:

- **`until=<Expr>`**: tested on each page. When it is truthy, that page is the last one (it is still kept). This is how a walk stops at the newest data: `until=wq.doc.select("time").attr("datetime") < "2026-09-01"`.
- **`filter=<Expr>`**: tested on each page. A page where it is falsy is skipped, and the walk goes on.
- **`max_pages=<n>`** (default 20): the page budget.
- **`records="<selector>"`**: the record selector. It measures progress for `click` / `scroll`, and it is what the walk compares to spot a repeated page.

A walk always stops, and says why:

| cause | when |
|---|---|
| `end` | no next link, no token, or `stop` reached |
| `empty` | a page came back empty or failed |
| `repeat` | a page repeated an earlier one: an out-of-range clamp. Compared by its records when `records` is given, else by its content without scripts. |
| `until` | `until` was truthy |
| `exhausted` | a click or scroll loaded nothing more |
| `budget` | `max_pages` was reached |

- **The tier carries over.** Later pages are fetched the way page one was: a page that needed the browser gets its next pages in the browser too.
- **The output** is a `Collection[Document]` of the kept pages, page one first. Chain `.select_all(record).extract(...).project()` and the rest of the plan runs across every page.

## What an LLM reads: `doc.dataset()`

The page's facts, in one place:

- **`paginated`**: the pagination hint.
  - `modes`: each has `mode`, `code` (the `.paginate(...)` to write), `evidence` and `confidence`. The first mode is the best.
  - Plus `total_pages` / `total_items` / `page_size` when a caption or a header reveals them.
- **`filtered`**: the active filter params and the filter controls. The listing is then a subset.
- **`ordered`**: the sort key and direction, whether the sort can be set, and its param.
- **`recipes`**: copyable plans:
  - `all`: the whole dataset (the best mode).
  - `latest`: just the newest data. It is page one when the listing is newest-first, else the whole dataset with `until`.
  - `unfiltered`: the same listing with its active filters removed.
  - `filtered`: how to apply a filter (a param on the reference).
- **`summary`**: the same, as a few lines of text for a prompt.

## The UI

Author and the plan view share one **pager editor** built on this spec:
- the iterator, chosen from the page's hints (each shown with its evidence);
- its expression, typed in or picked on the page ("this is the next link", "click this to load more");
- `until` / `filter` / `max_pages`.

Run shows the pages as the pager's items, and each record is numbered across them.
