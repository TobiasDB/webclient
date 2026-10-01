We want to scrape this dataset: $description.$fields_line
Here are the crawled pages (with detected flags like 'spa' = JS-rendered, 'paginated' = spans pages, 'auth_required' = gated):
$pages_json

$scope
Pick the pages worth evaluating as the source to scrape. The source is the LISTING that aggregates the records (an events calendar, a news index, a filings table, a catalogue) -- NEVER a single record (one article, one press release, one event's detail page): a single record is a leaf, not the dataset, and must not be picked even if it is the only page that looks relevant. For each, give its "url", a "kind" ("api" = an endpoint that RETURNS the records as data | "page" = a rendered listing | "spa" = a JS app), a "tier" ("must" | "should" | "could") by how likely+scrapeable it holds the WHOLE dataset, and a "reason" (one short sentence WHY you chose it).

Do NOT pick a page that merely DOCUMENTS or DESCRIBES an API (developer docs, API reference, guides, "/docs", "/developers") -- it describes an API, it is not the dataset. "kind":"api" means a live data endpoint, never its documentation.

Reply with ONLY a JSON array of such objects.
