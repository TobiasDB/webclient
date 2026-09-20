You are crawling $company's own website to reach this dataset: $description.$fields_line
From the frontier links below, choose the ones worth fetching next.

EXPLORE to reach the dataset's LISTING -- do not just pattern-match a URL. The dataset lives on a SECTION / INDEX / landing page that AGGREGATES the records (a newsroom, a "Press releases" or "News" listing, an investor-relations news index, a full catalogue). A homepage or a section landing page is rarely the dataset itself, but its navigation ("Investor Relations", "Newsroom", "News", "Press") LEADS to it -- so follow those section links; do NOT dismiss a homepage/landing page just because its URL doesn't look like data. Prefer a single queryable source of the WHOLE dataset (a data/export endpoint, an API that RETURNS the records) when one is offered, and follow pagination ('next', page N) when the listing spans pages.

DO NOT ENUMERATE. Never choose an INDIVIDUAL record -- a single article, one press release, a product detail page -- or a set of many similar item links. Those are leaves; the dataset is their LISTING page. Choose that ONE listing, not the items on it. Likewise skip nav chrome, legal, social, and account links. Pick FEW, high-value links (usually 1-3): the section/listing that holds the dataset, not a scatter of pages.

STAY ON $company: only choose links that belong to $company's own site/investor pages. If a link is about or belongs to a DIFFERENT company or organisation (a competitor, a partner, an aggregator, a news outlet), do NOT choose it -- we want $company's data, not anyone else's.

IMPORTANT: a page that DOCUMENTS or DESCRIBES an API -- developer docs, an API reference, integration guides, "/docs", "/developers", a Swagger/OpenAPI page -- is NOT the dataset. Do not head for it expecting data; the data is what a real endpoint RETURNS, not a page about one.

$listing

Reply with ONLY a JSON array, one object per chosen link: {"n": <link number>, "why": "<one short reason this link likely reaches the dataset>"}. Example: [{"n": 3, "why": "a /products.json data endpoint"}, {"n": 5, "why": "the full catalogue listing"}].
