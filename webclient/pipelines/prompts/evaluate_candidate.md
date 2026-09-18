Dataset wanted: $description.$fields_line
Candidate URL: $candidate_url
Detected flags (name: confidence): $flag_map_json
Observed data endpoints (XHR/fetch): $endpoints_json
Page skeleton:
$skeleton

Assess this page as the source to scrape. Judge from the skeleton what the page ACTUALLY is:
- "is_queryable" is true ONLY if this page IS a data endpoint (its body is the records as JSON/XML) OR it is backed by a same-origin data endpoint (listed above) that returns the records. A page that DOCUMENTS, DESCRIBES or lets you TRY an API -- developer docs, API reference, an OpenAPI/Swagger page, integration guides -- is NOT queryable and does NOT hold the dataset: set dataset_present=false, is_queryable=false and say so in the verdict.
- The records must be the dataset itself, not examples, code samples, or a description of the data.
- RECENCY (for a dated dataset -- news/press releases/filings): read the visible dates to judge the SORT ORDER, and find WHERE THE MOST RECENT records are. They are often split from older ones behind a control: YEAR TABS (an older year may be shown by default), a "Latest" vs "Archive" toggle, a category filter, or the first page. Clickable tab/filter controls are marked "← clickable", the repeating dataset "← RECORD LIST", on-demand records "[xhr]". Give a short "recency_hint" the query writer can act on.

Reply with ONLY a JSON object with keys: "dataset_present" (bool), "is_queryable" (bool, per the rule above), "api_endpoint" (str|null: if the records come from a same-origin data endpoint, the EXACT URL COPIED from the "Observed data endpoints" list above -- never invent one; null if the page itself is the source, so we scrape it directly), "sort_order" ("newest-first"|"oldest-first"|"unsorted"|null -- from the visible dates), "recency_hint" (str: where the MOST RECENT records are, for the query writer -- e.g. "records are newest-first, take the first ones", "the latest is in the '2026' year tab", "behind the 'Latest' filter", "on the first page"; "" if not a dated dataset), "completeness" ("full"|"partial"|"unknown"), "has_pagination" (bool), "has_filters" (bool), "dataset_is_subset" (bool: is our ask a subset of what is here?), "mostly_unstructured" (bool), "drilldown_links" (bool), "is_api_docs" (bool: is this API documentation rather than data?), "scrapability" (int 0-10), "verdict" (one short sentence explaining your judgement -- this is logged).
