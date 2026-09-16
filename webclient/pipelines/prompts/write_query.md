$guide

----
Using ONLY the query syntax above, write a query that extracts this dataset from the page: $description.$fields_line$pager
Base your CSS selectors on this page skeleton:
$skeleton

Your query MUST have all three parts: (1) select the repeating records with .select_all("<row selector>") -- this is REQUIRED, without it the query extracts nothing; (2) .extract(col=..., ...) each target field (nesting a sub-extract per nested schema branch); (3) end with .project(). Root it at wq.doc; do NOT write a fetch/resolve/reference -- only the extraction.

A field marked "(optional)" in the schema may not appear on every record: read it with .select("<selector>", optional=True) so a missing element yields null instead of dropping the whole record. Do NOT invent a value for it, and do NOT skip it -- just make it optional. A field NOT marked optional is expected on every record; pick a selector that reliably matches it.

Extract ONLY from what is in the skeleton. If a required field is NOT visible inside each record in the skeleton (e.g. a SKU/price/spec that only exists on the item's own page), the record's link points to it: follow that link with .attr("href").resolve() and select the field on the detail page -- do NOT substitute the link URL itself for the field, and do NOT guess an attribute (data-sku, etc.) that isn't shown. See the nested-resolve example in the guide.

Reply with ONLY the query code -- the wq.doc... chain itself, written exactly as in the guide, nothing else (no blob, no prose, no code fence).
