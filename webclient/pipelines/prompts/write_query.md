$guide

----
Using ONLY the query syntax above, write a query that extracts this dataset from the page: $description.$fields_line$pager
Base your CSS selectors on this page skeleton:
$skeleton

Your query MUST have all three parts: (1) select the repeating records with .select_all("<row selector>") -- this is REQUIRED, without it the query extracts nothing; (2) .extract(col=..., ...) each target field (nesting a sub-extract per nested schema branch); (3) end with .project(). Root it at wq.doc; do NOT wrap the WHOLE query in a fetch/reference or a leading .resolve() -- the pipeline supplies the source. (A per-record `.attr("href").resolve()` INSIDE .extract(), to follow a record's link to its detail page for a field, is expected and encouraged -- that is different from a leading resolve.)

A field marked "(optional)" in the schema may not appear on every record: read it with .select("<selector>", optional=True) so a missing element yields null instead of dropping the whole record. Do NOT invent a value for it, and do NOT skip it -- just make it optional. A field NOT marked optional is expected on every record; pick a selector that reliably matches it.

Extract ONLY from what is in the skeleton. If a required field is NOT visible inside each record in the skeleton (e.g. a SKU/price/spec that only exists on the item's own page), the record's link points to it: follow that link with .attr("href").resolve() and select the field on the detail page -- do NOT substitute the link URL itself for the field, and do NOT guess an attribute (data-sku, etc.) that isn't shown. See the nested-resolve example in the guide.

Capture the MOST RECENT data, not an archive. Many dated datasets (news, press releases, filings) split the current period from older ones behind a control: YEAR TABS (an older year often shows by default), a "Latest" vs "Archive" toggle, category filters, or a paginated first page. Do NOT scope your .select_all(...) to a single archived tab/section -- pick a record selector that matches the CURRENT/latest records (across all tabs, or the visible/first one which is usually newest). In the skeleton, clickable tab/filter controls are marked "← clickable", the repeating dataset is marked "← RECORD LIST", and records loaded on demand are marked "[xhr]"; use those to find where the newest records are.$hints

SPLIT DATASETS: if the data is spread across SEPARATE sections that do NOT share one record format (e.g. an "Upcoming" section that is a single differently-shaped row plus an "Archived" list), do NOT force one .select_all(...) over all of them. Instead write ONE simple, complete query PER section -- each a full wq.doc.select_all("<that section's record selector>").extract(...).project() -- and separate the sections with a line containing only three dashes (---). The pipeline runs each section query on its own and CONCATENATES the rows into one dataset, so a section with just ONE oddly-formatted row is captured by its own selector instead of being dropped by a selector tuned to the other section. Give every section query the SAME field columns (the target schema) so the combined rows are uniform; a section that turns out empty is fine. If, on the other hand, the sections DO share a record selector, don't split -- a single grouped comma selector (".upcoming .event, .past .event") is simpler.$recency

Reply with ONLY the query code -- the wq.doc... chain itself, written exactly as in the guide, nothing else (no blob, no prose, no code fence).
