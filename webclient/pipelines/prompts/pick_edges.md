You are crawling a company site to reach this dataset: $description.$fields_line
From the frontier links below, choose the ones worth fetching next. Prefer links that lead to a single queryable source of the WHOLE dataset (a data/export endpoint, an API that RETURNS the records, a full listing) over a page that shows only a slice. Follow pagination ('next', page N) when the dataset spans pages. Ignore nav chrome, legal, and social links.

IMPORTANT: a page that DOCUMENTS or DESCRIBES an API -- developer docs, an API reference, integration guides, "/docs", "/developers", a Swagger/OpenAPI page -- is NOT the dataset. Do not head for it expecting data; the data is what a real endpoint RETURNS, not a page about one.

$listing

Reply with ONLY a JSON array, one object per chosen link: {"n": <link number>, "why": "<one short reason this link likely reaches the dataset>"}. Example: [{"n": 3, "why": "a /products.json data endpoint"}, {"n": 5, "why": "the full catalogue listing"}].
